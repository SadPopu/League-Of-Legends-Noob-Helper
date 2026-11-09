"""Corre a app sem janela de terminal, com um ícone na zona de notificação do Windows.

Arranca com: pythonw -m coach.tray (é o que o run.bat faz). Sem terminal, as mensagens vão para data/noob-helper.log.
"""
import logging
import os
import socket
import sys
import threading
import webbrowser

from . import autostart, config

URL = f"http://127.0.0.1:{config.PORT}"
LOG_PATH = config.DATA_DIR / "noob-helper.log"


def already_running() -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", config.PORT)) == 0


def icon_image():
    """O escudo dourado do logo da app."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(32, 3), (56, 13), (56, 32), (50, 46), (32, 61), (14, 46), (8, 32), (8, 13)], fill=(200, 170, 110, 255))
    d.line([(21, 32), (29, 41), (44, 22)], fill=(5, 8, 12, 255), width=6, joint="curve")
    return img


def main(open_browser: bool = True):
    """open_browser=False é o arranque com o Windows: fica só o ícone, sem abrir o browser."""
    # abrir a app outra vez com ela já a correr só abre o browser (e não apaga o log da que está a correr)
    if already_running():
        if open_browser:
            webbrowser.open(URL)
        return

    # o pythonw não tem stdout/stderr: sem isto os logs do uvicorn rebentam ao arrancar
    sys.stdout = sys.stderr = open(LOG_PATH, "w", encoding="utf-8", buffering=1)

    import pystray
    import uvicorn
    from .main import app

    # uma linha por cada pedido à Riot encheria o log durante as recolhas de jogos
    logging.getLogger("httpx").setLevel(logging.WARNING)

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=config.PORT, timeout_graceful_shutdown=5))

    def quit_app(*_):
        server.should_exit = True  # ao parar o servidor, run_server fecha o ícone e o programa acaba

    def toggle_autostart(icon, _item):
        try:
            autostart.disable() if autostart.is_enabled() else autostart.enable()
        except OSError:
            logging.exception("Could not change the Windows startup entry")
        icon.update_menu()

    try:
        autostart.refresh()
    except OSError:
        logging.exception("Could not update the Windows startup entry")

    icon = pystray.Icon("noob-helper", icon_image(), "Noob Helper", pystray.Menu(
        pystray.MenuItem("Open Noob Helper", lambda *_: webbrowser.open(URL), default=True),
        pystray.MenuItem("Open log", lambda *_: os.startfile(LOG_PATH)),
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda _item: autostart.is_enabled()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", quit_app),
    ))

    def run_server():
        try:
            server.run()
        finally:
            icon.stop()

    def open_when_ready():
        while not server.started and not server.should_exit:
            threading.Event().wait(0.2)
        if server.started and open_browser:
            webbrowser.open(URL)

    def setup(icon):
        icon.visible = True
        threading.Thread(target=run_server, daemon=True).start()
        threading.Thread(target=open_when_ready, daemon=True).start()

    icon.run(setup=setup)


if __name__ == "__main__":
    main()
