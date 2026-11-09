"""Arrancar a app com o Windows: uma entrada só deste utilizador (HKCU), ligada e desligada no menu do ícone."""
import sys
import winreg
from pathlib import Path

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "NoobHelper"
ROOT = Path(__file__).resolve().parent.parent

# a entrada do registo não escolhe a pasta de trabalho, por isso é o próprio comando que vai para a pasta da app
BOOTSTRAP = "import os,sys; os.chdir(sys.argv[1]); sys.path.insert(0, '.'); from coach.tray import main; main(open_browser=False)"


def command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" -c "{BOOTSTRAP}" "{ROOT}"'


def is_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, VALUE)
        return True
    except FileNotFoundError:
        return False


def enable():
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        winreg.SetValueEx(key, VALUE, 0, winreg.REG_SZ, command())


def disable():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE)
    except FileNotFoundError:
        pass


def refresh():
    """Se a pasta da app mudou de sítio, a entrada ligada passa a apontar para a pasta nova."""
    if is_enabled():
        enable()
