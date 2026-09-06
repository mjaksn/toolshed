@echo off
cls
setlocal
:PROMPT

SET /P AREYOUSURE=[33mDo you want to reset all three local Ed-Fi repos (Y/[N])?[0m

IF /I "%AREYOUSURE%" NEQ "Y" GOTO DBDEL

echo.
echo [32mCleaning Ed-Fi-Extensions:[0m
echo.


cd C:\edfi\Ed-Fi-Extensions
git reset --hard
git clean -xfd

echo.
echo [32mCleaning Ed-Fi-ODS:[0m
echo.

cd C:\edfi\Ed-Fi-ODS
git reset --hard
git clean -xfd


echo.
echo [32mCleaning Ed-Fi-ODS-Implementation:[0m
echo.

cd C:\edfi\Ed-Fi-ODS-Implementation
git reset --hard
git clean -xfd

echo.
echo.
echo [34mDone cleaning.[0m




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


:DBDEL


SET /P AREYOUSURE=[33mDo you want to delete all Ed-Fi Databases (Y/[N])?[0m
IF /I "%AREYOUSURE%" NEQ "Y" GOTO END

echo [32mDeleting all local Ed-Fi databases...[0m


Set TheSQLCMDFileToExecute=%TEMP%\SQLCMDFile_delete_local_edfi.sql


(
echo|set /p=" EXEC sp_MSforeachdb 'IF DB_ID(''?'') > 4 AND ''[?]'' LIKE ''+[EdFi%%'' ESCAPE ''+'' BEGIN EXEC (''ALTER DATABASE [?] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [?]'' ) END';"
) > %TheSQLCMDFileToExecute% 

sqlcmd -s localhost -y 0 -i %TheSQLCMDFileToExecute%

echo [32mdone[0m



:END
echo.
pause 
endlocal
