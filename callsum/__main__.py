import hashlib
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QLibraryInfo, QLocale, QLockFile, QTranslator
from PySide6.QtGui import QIcon
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from . import __version__, config, osdeps, theme

# своя «почта» у каждой папки настроек: тестовая копия не достучится до основной
SERVER_NAME = f"{config.APP_ID}-{osdeps.user_id()}-{hashlib.md5(str(config.CONFIG_DIR).encode()).hexdigest()[:8]}"
USAGE = """call-summarizer [--hidden] [--toggle] [--bookmark] [--version] [--self-check [--full] [--report ФАЙЛ]]
  --hidden      запуститься свёрнутым в системный трей
  --toggle      начать или остановить запись (для горячей клавиши)
  --bookmark    отметить важный момент в идущей записи (для горячей клавиши)
  --version     показать версию
  --self-check  проверить, что всё нужное для работы на месте (--full — с пробным распознаванием)"""


def _send_to_running(command: str) -> bool:
    """Если приложение уже запущено, передаём ему команду и выходим."""
    sock = QLocalSocket()
    sock.connectToServer(SERVER_NAME)
    if not sock.waitForConnected(300):
        return False
    sock.write(command.encode())
    sock.waitForBytesWritten(1000)
    sock.disconnectFromServer()
    return True


def _quiet_stdio():
    """Сборка для Windows работает без консоли, и sys.stdout/stderr там None, а библиотеки
    (например, полоска загрузки модели Whisper) пишут в stderr — подставляем заглушку."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    return sys.stderr.name == os.devnull


def _log_to_file():
    """Вывод работающей копии без консоли — в app.log рядом с записями (пригодится при ошибках)."""
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        log = open(config.DATA_DIR / "app.log", "w", encoding="utf-8", buffering=1)
    except OSError:
        return
    sys.stdout = sys.stderr = log


def _single_instance(command: str) -> QLockFile | None:
    """Только одна копия программы. Замок захватывается в самом начале запуска: собранная
    программа загружается несколько секунд, и раньше вторая копия, запущенная в это время
    (повторный щелчок по ярлыку, автозапуск), не находила первую и запускалась тоже.

    None — программа уже работает (или ещё загружается): команда передана ей, этой копии
    нужно выйти."""
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(config.CONFIG_DIR / "instance.lock"))
    lock.setStaleLockTime(0)  # замок освобождается только вместе с процессом, который его держит
    if lock.tryLock(0):
        return lock
    # первая копия ещё загружается — ждём, пока она начнёт принимать команды
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if _send_to_running(command):
            return None
        if lock.tryLock(0):
            return lock  # та копия закрылась, так и не открывшись, — работаем сами
        time.sleep(0.3)
    return None


def main():
    args = sys.argv[1:]
    no_console = _quiet_stdio()
    osdeps.clean_child_env()
    osdeps.fix_ssl_certs()
    if "--help" in args or "-h" in args:
        print(USAGE)
        return 0
    if "--version" in args or "-V" in args:
        print(f"call-summarizer {__version__}")
        return 0
    if "--self-check" in args:
        from .selfcheck import run

        return run(args)
    command = "toggle" if "--toggle" in args else "bookmark" if "--bookmark" in args else "show"
    hidden = "--hidden" in args

    if osdeps.IS_WIN:
        # своё имя для Windows: по нему группируются окно на панели задач и уведомления
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("CallSummarizer.App")
    else:
        # системные диалоги открытия/сохранения файлов (KDE, GNOME) и определение тёмной темы
        os.environ.setdefault("QT_QPA_PLATFORMTHEME", "xdgdesktopportal")
    app = QApplication(sys.argv[:1])
    # так программа подписана в настройках трея KDE и в списке звуковых потоков
    app.setApplicationName(config.APP_NAME)
    app.setApplicationDisplayName(config.APP_NAME)
    app.setApplicationVersion(__version__)
    # по этому имени KDE связывает окно с ярлыком (иконка в панели задач)
    app.setDesktopFileName(config.APP_ID)
    app.setQuitOnLastWindowClosed(False)

    lock = _single_instance(command)
    if lock is None:
        return 0
    app._instance_lock = lock  # замок держится, пока работает программа
    if _send_to_running(command):
        return 0  # работает старая версия без замка — она и покажет окно
    if command == "bookmark":
        return 0  # программа не запущена — значит, и записи нет: отмечать нечего
    if no_console:
        _log_to_file()

    # русские подписи стандартных кнопок Qt («Сохранить», «Отмена» и т. п.)
    translator = QTranslator(app)
    if translator.load(QLocale("ru_RU"), "qtbase", "_", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)

    app.setFont(theme.ui_font())
    theme.apply(app, config.load().get("theme", "system"))

    icon = QIcon.fromTheme(config.ICON_NAME)
    if icon.isNull():
        icon = QIcon(str(Path(__file__).parent / "icon.svg"))

    from .ui import MainWindow

    win = MainWindow(icon)
    win.setup_hotkey(app)
    if not osdeps.IS_WIN:
        from . import kde_hotkey

        kde_hotkey.ensure_once()

    server = QLocalServer(app)
    QLocalServer.removeServer(SERVER_NAME)  # остаток от упавшего процесса
    server.listen(SERVER_NAME)

    def on_connection():
        sock = server.nextPendingConnection()
        if sock is None:
            return

        def read():
            win.handle_command(bytes(sock.readAll()).decode(errors="ignore").strip())
            sock.disconnectFromServer()

        if sock.bytesAvailable():
            read()
        else:
            sock.readyRead.connect(read)

    server.newConnection.connect(on_connection)

    # свёрнутым в трей запускаемся, только если трей есть — иначе окно не найти
    if not (hidden and win.tray_available()):
        win.show()
    if command == "toggle":
        win.handle_command("toggle")
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
