@echo off
REM Build script for yang-comparator-lite package (Windows)

echo Building yang-comparator-lite package...
echo.

REM Clean previous builds
echo Cleaning previous builds...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
if exist *.egg-info rmdir /s /q *.egg-info

REM Check for comparator module
echo Checking package structure...
if not exist "yang_rag\comparator" (
    echo Error: yang_rag\comparator directory not found!
    exit /b 1
)
echo Found comparator module

REM Check for compatibility rules
if not exist "yang_rag\comparator\compatibility_rules.xml" (
    echo Warning: compatibility_rules.xml not found!
) else (
    echo Found compatibility_rules.xml
)

REM Install build dependencies
echo.
echo Installing build dependencies...
python -m pip install --upgrade pip setuptools wheel build twine

REM Build the package
echo.
echo Building distributions...
python -m build -s -w

REM Check if build was successful
if exist "dist\" (
    echo.
    echo Build successful!
    echo.
    echo Built packages:
    dir dist\
    echo.
    echo Installation instructions:
    echo.
    echo   Local installation (for testing):
    echo     pip install dist\yang_comparator_lite-1.0.0-py3-none-any.whl
    echo.
    echo   Or install from source distribution:
    echo     pip install dist\yang-comparator-lite-1.0.0.tar.gz
    echo.
    echo   Upload to PyPI (requires account):
    echo     twine upload dist\*
    echo.
    echo Package ready for distribution!
) else (
    echo.
    echo Build failed! Check errors above.
    exit /b 1
)
