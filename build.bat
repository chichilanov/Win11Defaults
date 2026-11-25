@echo off
setlocal enabledelayedexpansion

echo ========================================
echo  Win11 Defaults Manager - Сборка
echo ========================================
echo.

:: Проверка Python
where python >nul 2>&1
if errorlevel 1 (
    echo [ОШИБКА] Python не найден в PATH!
    echo.
    echo Установите Python с https://www.python.org/downloads/
    echo ВАЖНО: при установке поставьте галочку "Add Python to PATH"
    echo.
    pause
    exit /b 1
)
echo [OK] Python найден:
python --version

:: Проверка PyInstaller
pip show pyinstaller >nul 2>&1
if errorlevel 1 (
    echo.
    echo [ВНИМАНИЕ] PyInstaller не установлен. Устанавливаю...
    pip install pyinstaller
    if errorlevel 1 (
        echo [ОШИБКА] Не удалось установить PyInstaller
        pause
        exit /b 1
    )
    echo [OK] PyInstaller установлен
) else (
    echo [OK] PyInstaller установлен
)

:: Проверка SetUserFTA.exe
if exist "SetUserFTA.exe" (
    echo [OK] SetUserFTA.exe найден
) else (
    echo.
    echo [ВНИМАНИЕ] SetUserFTA.exe не найден
    echo Скачайте вручную:
    echo https://kolbi.cz/blog/2011/10/27/setuserfta-userchoice-hash-defeated-set-file-type-associations-per-user/
    echo.
    echo Сборка продолжится без SetUserFTA.exe
    echo (программа попросит указать путь при первом запуске)
    echo.
)

:: Проверка основного файла
if not exist "win11_defaults.py" (
    echo.
    echo [ОШИБКА] Файл win11_defaults.py не найден
    pause
    exit /b 1
)
echo [OK] win11_defaults.py найден

echo.
echo ========================================
echo  Начало сборки...
echo ========================================
echo.

:: Очистка предыдущих сборок
if exist "build" rmdir /s /q build
if exist "dist" rmdir /s /q dist
if exist "*.spec" del /q *.spec

:: Сборка GUI версии
echo [1/2] Сборка GUI версии (Win11Defaults.exe)...
if exist "SetUserFTA.exe" (
    pyinstaller -F -w --uac-admin --add-data "SetUserFTA.exe;." --name Win11Defaults win11_defaults.py
) else (
    pyinstaller -F -w --uac-admin --name Win11Defaults win11_defaults.py
)
echo.

:: Сборка CLI версии
echo [2/2] Сборка CLI версии (Win11DefaultsCLI.exe)...
if exist "SetUserFTA.exe" (
    pyinstaller -F --uac-admin --add-data "SetUserFTA.exe;." --name Win11DefaultsCLI win11_defaults.py
) else (
    pyinstaller -F --uac-admin --name Win11DefaultsCLI win11_defaults.py
)
echo.

:: Результаты
echo ========================================
echo  Сборка завершена!
echo ========================================
echo.
echo Созданные файлы:
if exist "dist\Win11Defaults.exe" (
    echo   [OK] dist\Win11Defaults.exe
) else (
    echo   [!!] dist\Win11Defaults.exe НЕ СОБРАН
)
if exist "dist\Win11DefaultsCLI.exe" (
    echo   [OK] dist\Win11DefaultsCLI.exe
) else (
    echo   [!!] dist\Win11DefaultsCLI.exe НЕ СОБРАН
)
echo.

:: Создание архива
set /p create_zip="Создать ZIP-архив для распространения? (Y/N): "
if /i "!create_zip!"=="Y" (
    echo.
    echo Создание архива...
    
    if exist "Win11Defaults_Release" rmdir /s /q Win11Defaults_Release
    mkdir Win11Defaults_Release
    
    if exist "dist\Win11Defaults.exe" copy "dist\Win11Defaults.exe" "Win11Defaults_Release\" >nul
    if exist "dist\Win11DefaultsCLI.exe" copy "dist\Win11DefaultsCLI.exe" "Win11Defaults_Release\" >nul
    
    if exist "Win11Defaults-Manual.html" (
        copy "Win11Defaults-Manual.html" "Win11Defaults_Release\" >nul
        echo [OK] Добавлена документация
    )
    if exist "README.txt" (
        copy "README.txt" "Win11Defaults_Release\" >nul
        echo [OK] Добавлен README
    )
    
    echo Упаковка в ZIP...
    powershell -Command "Compress-Archive -Path 'Win11Defaults_Release\*' -DestinationPath 'Win11Defaults_Release.zip' -Force"
    
    if exist "Win11Defaults_Release.zip" (
        echo.
        echo [OK] Архив создан: Win11Defaults_Release.zip
    ) else (
        echo.
        echo [ОШИБКА] Не удалось создать архив
    )
    
    rmdir /s /q Win11Defaults_Release
) else (
    echo.
    echo Архив не создан. Файлы находятся в папке dist\
)

echo.
echo ========================================
echo  Готово!
echo ========================================
echo.
pause