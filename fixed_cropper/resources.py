from pathlib import Path
import sys

def app_root() -> Path:
    """
    実行環境に応じたアプリのルートパスを返す。
    - 通常実行：プロジェクトルート
    - PyInstaller(onefile)：展開先(sys._MEIPASS)
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent

def asset_path(*parts: str) -> str:
    """
    (プロジェクトルート)/assets/... を指す
    """
    return str(app_root() / "assets" / Path(*parts))

def resource_path(*parts: str) -> str:
    """
    fixed_cropper/resources/... を指す
    - 通常実行： (このresources.pyがあるフォルダ)/resources/...
      => fixed_cropper/resources/...
    - PyInstaller：展開先に fixed_cropper/resources が入る想定
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS) / "fixed_cropper"
    else:
        base = Path(__file__).resolve().parent  # fixed_cropper/ フォルダ
    return str(base / "resources" / Path(*parts))
