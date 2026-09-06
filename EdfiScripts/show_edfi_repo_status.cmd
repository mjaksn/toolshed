@echo off
cls
setlocal



SET /P SHOWITALL=[33mShow everything (including ignored and untracked files)? (Y/[N])?[0m
IF /I "%SHOWITALL%" NEQ "Y" GOTO SHOWSOME

echo.
echo [32mCurrent Ed-Fi-Extensions status:[0m
echo.

cd C:\edfi\Ed-Fi-Extensions
git status --ignored -u

echo.
echo [32mCurrent Ed-Fi-ODS status:[0m
echo.

cd C:\edfi\Ed-Fi-ODS
git status --ignored -u

echo.
echo [32mCurrent Ed-Fi-ODS-Implementation status:[0m
echo.

cd C:\edfi\Ed-Fi-ODS-Implementation
git status --ignored -u

echo.


GOTO END

:SHOWSOME


echo.
echo [32mCurrent Ed-Fi-Extensions status:[0m
echo.

cd C:\edfi\Ed-Fi-Extensions
git status

echo.
echo [32mCurrent Ed-Fi-ODS status:[0m
echo.

cd C:\edfi\Ed-Fi-ODS
git status

echo.
echo [32mCurrent Ed-Fi-ODS-Implementation status:[0m
echo.

cd C:\edfi\Ed-Fi-ODS-Implementation
git status

echo.


:END

pause
endlocal
