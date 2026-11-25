═══════════════════════════════════════════════════════════
  Win11 Defaults Manager - Инструкция по сборке
═══════════════════════════════════════════════════════════

ТРЕБОВАНИЯ:
  - Windows 10/11
  - Python 3.9 или новее (https://www.python.org/downloads/)
    При установке ОБЯЗАТЕЛЬНО отметьте "Add Python to PATH"
  - PyInstaller (устанавливается автоматически скриптом)
  - SetUserFTA.exe (скачивается вручную, см. ниже)

ПОДГОТОВКА:
  1. Установите Python с https://www.python.org/downloads/
     ВАЖНО: При установке поставьте галочку "Add Python to PATH"
  
  2. Скачайте SetUserFTA.exe:
     https://kolbi.cz/blog/2011/10/27/setuserfta-userchoice-hash-defeated-set-file-type-associations-per-user/
     
     Прямая ссылка на файл:
     https://www.winhelponline.com/blog/wp-content/uploads/SetUserFTA.zip
     
     Распакуйте SetUserFTA.exe из архива и положите в эту папку.
  
  3. Убедитесь, что в папке есть файлы:
     - win11_defaults.py
     - SetUserFTA.exe
     - build.bat
     - README.txt (этот файл)

СБОРКА:
  1. Откройте командную строку (cmd) в этой папке
     (Правый клик по папке → "Открыть в терминале" или "Открыть окно команд")
  
  2. Запустите:
     build.bat
  
  3. Дождитесь завершения сборки (2-5 минут)
  
  4. Готовые файлы появятся в папке dist\

РЕЗУЛЬТАТ СБОРКИ:
  dist\Win11Defaults.exe      - GUI версия (двойной клик для запуска)
  dist\Win11DefaultsCLI.exe   - CLI версия (для командной строки)

СОЗДАНИЕ АРХИВА:
  После сборки скрипт предложит создать ZIP-архив.
  Архив будет содержать:
    - Оба exe файла
    - Документацию (если есть Win11Defaults-Manual.html)
    - Этот README.txt

УСТРАНЕНИЕ НЕПОЛАДОК:

  Ошибка: "Python не найден"
  Решение: Переустановите Python, отметив "Add Python to PATH"

  Ошибка: "SetUserFTA.exe не найден"
  Решение: Скачайте вручную по ссылке выше и положите в папку

  Ошибка: "tkinter не найден" при сборке GUI
  Решение: Переустановите Python с компонентом tcl/tk

  Ошибка: "Антивирус удаляет exe"
  Решение: Добавьте папку в исключения антивируса

  Ошибка: "Сборка завершена с ошибками"
  Решение: Проверьте, что win11_defaults.py не повреждён

  Ошибка: "Архив не создан"
  Решение: Создайте архив вручную из папки dist\

ПРОВЕРКА РАБОТОСПОСОБНОСТИ:
  1. Запустите dist\Win11Defaults.exe - должно открыться окно программы
  2. Запустите dist\Win11DefaultsCLI.exe --help - должна появиться справка

ПОЛЕЗНЫЕ ССЫЛКИ:
  - Python: https://www.python.org/
  - SetUserFTA: https://kolbi.cz/blog/2011/10/27/setuserfta-userchoice-hash-defeated-set-file-type-associations-per-user/
  - PyInstaller: https://www.pyinstaller.org/
  - Документация: Win11Defaults-Manual.html (если есть)

═══════════════════════════════════════════════════════════