from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def find_blender_executable() -> str | None:
    configured = os.environ.get("BLENDER_EXE")
    if configured:
        return configured if Path(configured).is_file() or shutil.which(configured) else None
    installed = shutil.which("blender")
    if installed:
        return installed
    if os.name == "nt":
        try:
            process = subprocess.run([
                "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
                "Get-Process -Name blender -ErrorAction SilentlyContinue | "
                "Select-Object -First 1 -ExpandProperty Path",
            ], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5, check=False)
            running = process.stdout.strip().lstrip("\ufeff")
            if process.returncode == 0 and running and Path(running).is_file():
                return running
        except (OSError, subprocess.TimeoutExpired):
            pass
        for variable in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            folder = os.environ.get(variable)
            if not folder:
                continue
            base = Path(folder)
            for parent in (base / "Blender Foundation", base / "Programs/Blender Foundation"):
                for executable in sorted(parent.glob("Blender*/blender.exe"), reverse=True):
                    if executable.is_file():
                        return str(executable)
            steam = base / "Steam/steamapps/common/Blender/blender.exe"
            if steam.is_file():
                return str(steam)
    mac = Path("/Applications/Blender.app/Contents/MacOS/Blender")
    return str(mac) if mac.is_file() else None


def write_case_launcher(output: Path) -> tuple[Path, Path]:
    launcher = output / "generate_blend.bat"
    script = r'''@echo off
setlocal
set "case_blender=%BLENDER_EXE%"
if not "%~1"=="" set "case_blender=%~1"
if not defined case_blender for /f "delims=" %%B in ('where blender.exe 2^>nul') do if not defined case_blender set "case_blender=%%B"
if not defined case_blender for /f "delims=" %%B in ('powershell.exe -NoProfile -Command "Get-Process -Name blender -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path"') do set "case_blender=%%B"
if not defined case_blender for /d %%D in ("%ProgramFiles%\Blender Foundation\Blender*" "%ProgramFiles(x86)%\Blender Foundation\Blender*" "%LOCALAPPDATA%\Programs\Blender Foundation\Blender*") do if exist "%%~D\blender.exe" set "case_blender=%%~D\blender.exe"
if not defined case_blender if exist "%ProgramFiles(x86)%\Steam\steamapps\common\Blender\blender.exe" set "case_blender=%ProgramFiles(x86)%\Steam\steamapps\common\Blender\blender.exe"
if not defined case_blender goto missing
if not exist "%case_blender%" goto missing
if exist "%~dp0scene.blend" (
  choice /c YN /m "scene.blend already exists. Rebuild it?"
  if errorlevel 2 exit /b 0
)
"%case_blender%" --background --factory-startup --python-exit-code 1 --python "%~dp0blender_render.py" -- --manifest "%~dp0blender_manifest.json" --blend "%~dp0scene.blend"
if errorlevel 1 goto failed
echo.
echo Ready: "%~dp0scene.blend"
echo Open scene.blend using Blender File - Open.
pause
exit /b 0
:missing
echo Blender was not found. Open Blender and try again, or drag blender.exe onto this file.
pause
exit /b 1
:failed
echo Blender could not generate the scene. Review the error above.
pause
exit /b 1
'''
    launcher.write_bytes(script.replace("\n", "\r\n").encode("utf-8"))
    readme = output / "README_BLENDER.txt"
    readme.write_text(
        "SceneFactory Blender Case\n\n"
        "本目录包含场景清单、生成脚本和 GLB 资产。Blender 的‘文件 → 打开’只打开 .blend，不能直接打开这些原始文件。\n\n"
        "Windows：解压整个 ZIP，双击 generate_blend.bat，完成后在 Blender 中打开 scene.blend。\n"
        "需要已安装 Blender，不需要额外安装 Python。若自动定位失败，先打开 Blender 再重试，或把 blender.exe 拖到该脚本上。\n"
        "也可以设置 BLENDER_EXE 为 Blender 可执行文件路径。已有 scene.blend 时会询问是否重新生成。\n\n"
        "macOS/Linux：在此目录运行：\n"
        "blender --background --factory-startup --python-exit-code 1 --python blender_render.py -- --manifest blender_manifest.json --blend scene.blend\n\n"
        "如果 ZIP 中已包含 scene.blend，可直接使用 Blender 的‘文件 → 打开’。\n"
        "默认使用材质预览，隐藏辅助坐标、灯光轮廓和父子连线；可在视图的叠加层设置中重新显示。\n"
        "未提供 GLB 的资产仍使用方块占位外观。模型是可视化场景，不表示机器人或物理任务已验收。\n",
        encoding="utf-8",
    )
    return launcher, readme
