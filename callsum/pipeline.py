"""Фоновая очередь обработки: импорт → распознавание → саммари."""

import hashlib
import queue
import threading
import traceback
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from . import audio, calendar_ics, config, llm, speakers, summarize, templates, transcribe, voices
from .storage import Call


class Pipeline(QObject):
    # call_id, текст, доля 0..1 (или -1, если прогресс неизвестен)
    status = Signal(str, str, float)
    summary_chunk = Signal(str, str)
    # call_id, этап
    stage_done = Signal(str, str)
    # call_id, этап, сообщение
    failed = Signal(str, str, str)
    idle = Signal()

    def __init__(self, get_config: Callable[[], dict]):
        super().__init__()
        self._get_config = get_config
        self._queue: queue.Queue = queue.Queue()
        self._cancel: set[str] = set()
        self._pending: set[str] = set()
        self.current: str | None = None
        threading.Thread(target=self._loop, daemon=True).start()

    def is_busy(self, call_id: str | None = None) -> bool:
        if call_id is None:
            return bool(self._pending)
        return call_id in self._pending

    def enqueue(self, call: Call, stages: list[str], src: Path | None = None):
        self._cancel.discard(call.id)
        self._pending.add(call.id)
        call.save(status="queued", error="")
        self._queue.put((call, stages, src))

    def cancel(self, call_id: str):
        if call_id in self._pending:
            self._cancel.add(call_id)

    def _loop(self):
        while True:
            call, stages, src = self._queue.get()
            self.current = call.id
            cancelled = lambda cid=call.id: cid in self._cancel  # noqa: E731
            stage = ""
            try:
                for stage in stages:
                    if cancelled():
                        raise transcribe.Cancelled()
                    getattr(self, f"_do_{stage}")(call, src, cancelled)
                    self.stage_done.emit(call.id, stage)
            except (transcribe.Cancelled, speakers.Cancelled):
                self._restore_status(call)
                self.failed.emit(call.id, stage, "Отменено.")
            except Exception as e:  # noqa: BLE001 — показываем пользователю любую ошибку
                traceback.print_exc()
                msg = str(e) or e.__class__.__name__
                if cancelled():
                    self._restore_status(call)
                else:
                    call.save(status="error", error=msg)
                self.failed.emit(call.id, stage, msg)
            finally:
                self._pending.discard(call.id)
                self._cancel.discard(call.id)
                self.current = None
                if not self._pending:
                    self.idle.emit()

    @staticmethod
    def _restore_status(call: Call):
        if call.summary():
            call.save(status="done")
        elif call.has_transcript():
            call.save(status="transcribed")
        else:
            call.save(status="recorded")

    def _emit(self, call: Call, text: str, frac: float | None):
        self.status.emit(call.id, text, -1.0 if frac is None else frac)

    # этапы

    def _do_import(self, call: Call, src: Path | None, cancelled):
        assert src is not None
        call.save(status="importing")
        self._emit(call, f"Извлечение звука из «{src.name}»…", None)
        dst = call.path / "audio.ogg"
        audio.extract_audio(src, dst)
        call.save(duration=audio.probe_duration(dst), original_file=str(src))

    def _do_transcribe(self, call: Call, _src, cancelled):
        cfg = self._get_config()
        tracks = call.tracks()
        if not tracks:
            raise RuntimeError("У этого созвона нет аудиозаписи.")
        call.save(status="transcribing")
        self._match_calendar(call, cfg)
        words = transcribe.transcribe(
            tracks,
            model_name=cfg["whisper_model"],
            language=cfg["language"],
            on_status=lambda text, frac: self._emit(call, text, frac),
            cancelled=cancelled,
            vocabulary=config.vocabulary_terms(cfg),
            device=cfg.get("whisper_device", "auto"),
            denoise=bool(cfg.get("denoise")),
        )
        call.save(whisper_device=transcribe.device_used)
        if len(tracks) > 1 and call.playback_path() is None:
            self._emit(call, "Подготовка записи для прослушивания…", None)
            audio.make_playback(tracks, call.path / "playback.ogg")
        if not words:
            raise RuntimeError("Речь в записи не найдена. Проверьте, что звук действительно записался.")
        call.write_words(words)
        # транскрипт без разметки появляется сразу, говорящих расставит следующий этап
        call.write_paragraphs(speakers.build_paragraphs(words, [None] * len(words)), reset_names=True)
        call.save(status="transcribed")

    def _do_speakers(self, call: Call, _src, cancelled):
        cfg = self._get_config()
        tracks = call.tracks()
        words = call.words()
        if not words:
            return
        call.save(status="diarizing")
        # запись созвона: «Я» — по дорожке микрофона, собеседники — по голосу на своей дорожке
        me_by_track = "mic" in tracks and "system" in tracks
        source = tracks.get("system") if me_by_track else next(iter(tracks.values()), None)
        turns = None
        if source is not None and (cfg["diarize"] or me_by_track):
            self._emit(call, "Подготовка звука…", None)
            audio_data = transcribe.decode(source)
            if cfg["diarize"]:
                models = speakers.ensure_models(lambda text, frac: self._emit(call, text, frac))
                label = "Определение говорящих по голосу…"
                self._emit(call, label, 0.0)
                wanted = int(call.meta.get("num_speakers") or 0)
                turns = speakers.diarize(
                    audio_data, wanted,
                    lambda frac: self._emit(call, label, frac), cancelled, models,
                )
                if not wanted:
                    # один человек нередко распадается на нескольких «спикеров» — склеиваем похожие голоса;
                    # если в календаре известен список приглашённых, больше их спикеров быть не должно
                    self._emit(call, "Объединение похожих голосов…", None)
                    invited = len((call.meta.get("calendar") or {}).get("attendees") or [])
                    turns = speakers.merge_clusters(turns, audio_data, models,
                                                    max_speakers=max(0, invited - 1) if me_by_track else invited)
            else:
                turns = speakers.activity_turns(audio_data)
        labels = speakers.label_words(words, turns, me_by_track) if turns is not None else [None] * len(words)
        paragraphs = speakers.build_paragraphs(words, labels)
        call.write_paragraphs(paragraphs, reset_names=True)
        if cfg["diarize"] and source is not None:
            self._recognize_voices(call, cfg, paragraphs, tracks, me_by_track, audio_data, models)
        call.save(status="transcribed")

    def _recognize_voices(self, call: Call, cfg: dict, paragraphs, tracks, me_by_track, source_audio, models):
        """Считает «отпечатки» голосов и подписывает тех, кого уже знаем."""
        self._emit(call, "Сравнение голосов с запомненными…", None)
        mic_audio = transcribe.decode(tracks["mic"]) if me_by_track else None

        def audio_for(sid):
            return mic_audio if sid == speakers.ME else source_audio

        embs = speakers.voice_embeddings(paragraphs, audio_for, models)
        call.write_voice_embeddings(embs)
        if not cfg.get("remember_voices", True):
            return
        if me_by_track and speakers.ME in embs and not call.meta.get("own_voice_saved"):
            # свой голос в записи с микрофона известен точно — пополняем профиль «Я» (один раз на созвон)
            voices.remember(voices.OWN_VOICE, embs[speakers.ME])
            call.save(own_voice_saved=True)
        remote = {sid: e for sid, e in embs.items() if sid != speakers.ME}
        # на дорожке собеседников вашего голоса нет — не даём спутать собеседника с вами
        exclude = {voices.OWN_VOICE} if me_by_track else set()
        by_name: dict[str, list[str]] = {}
        for sid, (name, _sim) in voices.match_all(remote, exclude).items():
            by_name.setdefault(name, []).append(sid)
        talk = speakers.talk_time(paragraphs)
        for name, sids in by_name.items():
            keep = max(sids, key=lambda s: talk.get(s, 0.0))
            # несколько говорящих узнаны как один человек — это он, разбитый на части;
            # склеиваем только тех, кто похож и друг на друга, остальных не трогаем
            same = [s for s in sids if s != keep and voices.similarity(remote[s], remote[keep]) >= 0.5]
            if same:
                call.merge_speakers(same, keep)
            call.rename_speaker(keep, name, auto=True, source="voice")

    def _match_calendar(self, call: Call, cfg: dict):
        """Ищем в календаре встречу, во время которой шла запись: её название и участники."""
        sources = calendar_ics.urls(cfg)
        if call.meta.get("source") != "recording" or not sources:
            return
        key = hashlib.sha1("\n".join(sources).encode()).hexdigest()[:12]
        if call.meta.get("calendar_key") == key:
            return  # уже искали в этих календарях
        try:
            event = calendar_ics.find_meeting(cfg, call.created, call.duration)
        except Exception:  # noqa: BLE001 — календарь необязателен, его ошибки не должны мешать обработке
            traceback.print_exc()
            return
        call.save(calendar=calendar_ics.describe(event) if event else {}, calendar_key=key)
        if event and not call.meta.get("title_manual"):
            call.save(title=event.title[:120], title_source="calendar")

    @staticmethod
    def _bookmarks_note(call: Call) -> str:
        marks = call.bookmarks()
        if not marks:
            return ""
        items = ", ".join(f"[{transcribe._ts(b['t'])}]" + (f" («{b['note']}»)" if b.get("note") else "")
                          for b in marks)
        return ("Во время созвона пользователь отметил важные моменты: " + items + ". После раздела «Кратко» "
                "добавь раздел «## Отмеченные моменты»: для каждой отметки — её таймкод и что обсуждалось "
                "в этот момент (отметку обычно ставят через несколько секунд после важной фразы).")

    def _name_speakers(self, call: Call, cfg: dict, transcript: str):
        """Просит модель узнать имена участников из разговора; уверенные — сразу подставляет."""
        names = call.speaker_names()
        defaults = speakers.default_names(list(names))
        unnamed = [sid for sid in names if sid.startswith("S") and names[sid] == defaults[sid]]
        if not unnamed or call.meta.get("names_checked"):
            return
        self._emit(call, f"{llm.who(cfg)} определяет имена участников…", None)
        invited = (call.meta.get("calendar") or {}).get("attendees") or []
        hint = ("По приглашению из календаря на встрече были: " + ", ".join(invited) + ". Если по разговору "
                "видно, кто из них говорит под меткой, используй это имя.") if invited else ""
        try:
            found = summarize.identify_speakers(
                cfg, transcript, [defaults[sid] for sid in unnamed],
                vocabulary=config.vocabulary_terms(cfg), hint=hint,
            )
        except llm.LLMError:
            return  # без имён саммари всё равно получится
        by_label = {defaults[sid]: sid for sid in unnamed}
        suggestions = dict(call.meta.get("speaker_suggestions") or {})
        for label, (name, confident) in found.items():
            sid = by_label[label]
            if confident:
                call.rename_speaker(sid, name, auto=True, source="claude")
            else:
                suggestions[sid] = name
        call.save(speaker_suggestions=suggestions, names_checked=True)

    def _do_summarize(self, call: Call, _src, cancelled):
        cfg = self._get_config()
        if not call.paragraphs():
            raise RuntimeError("Транскрипт пустой — сначала распознайте запись.")
        call.save(status="summarizing")
        self._match_calendar(call, cfg)
        transcript = call.label_transcript()
        self._name_speakers(call, cfg, transcript)
        if cancelled():
            raise transcribe.Cancelled()

        names = call.speaker_names()
        defaults = speakers.default_names(list(names))
        known = [f"{defaults[sid]} — {names[sid]}" for sid in names if names[sid] != defaults[sid]]
        names_note = ("Имена участников (метка в транскрипте — имя): " + "; ".join(known) + ". "
                      "В саммари называй людей по именам.") if known else ""

        choice = call.meta.get("template_choice") or cfg.get("summary_template") or templates.AUTO
        self._emit(call, f"{llm.who(cfg)} пишет саммари…", None)
        text = summarize.summarize(
            cfg, transcript,
            instructions=templates.instructions(choice, cfg["summary_prompt"], call.created),
            on_text=lambda chunk: self.summary_chunk.emit(call.id, chunk),
            cancelled=cancelled,
            notes=[names_note, calendar_ics.note(call.meta.get("calendar") or {}), self._bookmarks_note(call)],
            vocabulary=config.vocabulary_terms(cfg),
        )
        text, used = templates.split_marker(text)
        call.write_summary(text)
        call.save(status="done", summary_model=cfg["claude_model"] if llm.backend(cfg) == "claude" else "",
                  summary_label=llm.model_label(cfg), template=used if choice == templates.AUTO else choice)
        from . import export

        export.auto_obsidian(call, cfg)
