@echo off
setlocal EnableExtensions DisableDelayedExpansion
pushd "%~dp0" || exit /b 1
set "STEP=verify bundle and Docker Desktop"
set "STARTED="
set "LOG="
findstr /x /l "afc1b53620fe8961ce492f4d1ed1ef7be0d627ab" source-revision >nul || goto :failed
set "SUFFIX=afc1b53620fe"
set "PROJECT=ea-win-rehearsal-%RANDOM%%RANDOM%%RANDOM%"
set "WORK=%TEMP%\%PROJECT%"
mkdir "%WORK%" || goto :failed
set "LOG=%WORK%\result.log"
set "DB=enterprise_agent_rehearsal"
set "RESTORED_DB=enterprise_agent_restored"
set "EA_DEPLOY_DOMAIN=release-test.invalid"
set "EA_FUNNEL_ORIGIN_PORT=28080"
set "EA_DEPLOY_ENV_FILE=%WORK%\application.env"
set "EA_DATABASE_USER=enterprise_agent"
set "EA_DATABASE_NAME=%DB%"
set "EA_RELEASE_API_IMAGE=ea-windows-rehearsal-api:%SUFFIX%"
set "EA_RELEASE_WEB_IMAGE=ea-windows-rehearsal-web:%SUFFIX%"
set "EA_WINDOWS_TEST_DB_IMAGE=ea-windows-rehearsal-db:%SUFFIX%"
set "EA_WINDOWS_TEST_INIT_IMAGE=ea-windows-rehearsal-init:%SUFFIX%"
set "BASELINE=ea-windows-rehearsal-baseline:%SUFFIX%"
set "CANDIDATE=%EA_RELEASE_API_IMAGE%"
type nul > "%EA_DEPLOY_ENV_FILE%"
docker --context desktop-linux version --format "{{.Server.Os}}/{{.Server.Arch}}" > "%WORK%\engine.txt" 2>>"%LOG%" || goto :failed
findstr /x /l "linux/amd64" "%WORK%\engine.txt" >nul || goto :failed
call :compose config --quiet >>"%LOG%" 2>&1 || goto :failed

set "STEP=load and verify five isolated images"
docker --context desktop-linux image load --input images.tar.gz >>"%LOG%" 2>&1 || goto :failed
for %%I in (api web db init baseline) do (
    call :verify_image %%I || goto :failed
)

rem 使用随机项目名和独立数据库，只在本机回环端口提供测试入口。
set "STEP=start baseline 0026 and create backup"
set "EA_RELEASE_API_IMAGE=%BASELINE%"
set "STARTED=1"
call :compose up -d --no-build --pull never --wait --wait-timeout 180 api >>"%LOG%" 2>&1 || goto :failed
call :assert_sql "%DB%" "SELECT version_num FROM alembic_version" "0026" || goto :failed
call :sql "%DB%" -c "INSERT INTO conversations (id, title, next_message_sequence) VALUES ('20000000-0000-0000-0000-000000000001', 'windows-release-rehearsal', 2); INSERT INTO messages (id, conversation_id, sequence, role, content, status) VALUES ('20000000-0000-0000-0000-000000000002', '20000000-0000-0000-0000-000000000001', 1, 'user', 'windows-release-rehearsal-message', 'completed')" >>"%LOG%" 2>&1 || goto :failed
call :compose stop api >>"%LOG%" 2>&1 || goto :failed
call :compose exec -T db pg_dump -U "%EA_DATABASE_USER%" -d "%DB%" -Fc > "%WORK%\pre-upgrade.dump" 2>>"%LOG%" || goto :failed
call :compose exec -T db pg_restore --list < "%WORK%\pre-upgrade.dump" >>"%LOG%" 2>&1 || goto :failed

set "STEP=switch to 0027 and check existing data"
set "EA_RELEASE_API_IMAGE=%CANDIDATE%"
call :compose up -d --no-build --pull never --wait --wait-timeout 180 web >>"%LOG%" 2>&1 || goto :failed
call :check_web || goto :failed
call :assert_sql "%DB%" "SELECT version_num FROM alembic_version" "0027" || goto :failed
call :assert_sql "%DB%" "SELECT to_regclass('public.chat_reports') IS NOT NULL" "t" || goto :failed
call :check_message "%DB%" || goto :failed

rem 用空库恢复升级前的数据，保留已迁移的数据库供比对。
set "STEP=restore 0026 and switch back the test API"
call :compose exec -T db createdb -U "%EA_DATABASE_USER%" "%RESTORED_DB%" >>"%LOG%" 2>&1 || goto :failed
call :compose exec -T db pg_restore --exit-on-error --single-transaction --no-owner --no-privileges -U "%EA_DATABASE_USER%" -d "%RESTORED_DB%" < "%WORK%\pre-upgrade.dump" >>"%LOG%" 2>&1 || goto :failed
call :assert_sql "%RESTORED_DB%" "SELECT version_num FROM alembic_version" "0026" || goto :failed
call :check_message "%RESTORED_DB%" || goto :failed
set "EA_DATABASE_NAME=%RESTORED_DB%"
set "EA_RELEASE_API_IMAGE=%BASELINE%"
call :compose up -d --no-deps --no-build --pull never --wait --wait-timeout 180 api >>"%LOG%" 2>&1 || goto :failed
call :check_web || goto :failed
call :assert_sql "%DB%" "SELECT version_num FROM alembic_version" "0027" || goto :failed

set "STEP=stop isolated services"
call :compose stop web api db >>"%LOG%" 2>&1 || goto :failed
echo PASS: Windows Docker Desktop upgrade 0026 to 0027 and restore to 0026.
echo Evidence: "%LOG%"
echo Project: %PROJECT%
>>"%LOG%" echo PASS: Windows Docker Desktop upgrade 0026 to 0027 and restore to 0026.
popd
exit /b 0

:failed
echo FAIL: %STEP%
if defined LOG echo Evidence: "%LOG%"
if defined STARTED call :compose logs --tail=60 db api web >>"%LOG%" 2>&1
if defined STARTED call :compose stop web api db >>"%LOG%" 2>&1
popd
exit /b 1

:compose
docker --context desktop-linux compose -p "%PROJECT%" --env-file "%EA_DEPLOY_ENV_FILE%" -f compose.yaml -f compose.funnel.yaml -f compose.release.yaml -f compose.release-test.yaml -f compose.windows-release-test.yaml %*
exit /b %errorlevel%

:sql
rem 保留 SQL 的引号，避免 CMD 将语句拆成多个参数。
call :compose exec -T db psql -X -At -v ON_ERROR_STOP=1 -U "%EA_DATABASE_USER%" -d "%~1" %2 %3
exit /b %errorlevel%

:assert_sql
call :sql "%~1" -c "%~2" > "%WORK%\query.txt" 2>>"%LOG%" || exit /b 1
findstr /x /l /c:"%~3" "%WORK%\query.txt" >nul
exit /b %errorlevel%

:check_message
call :assert_sql "%~1" "SELECT content FROM messages WHERE id='20000000-0000-0000-0000-000000000002'" "windows-release-rehearsal-message"
exit /b %errorlevel%

:check_web
curl.exe --fail --silent --show-error --retry 8 --retry-connrefused --retry-delay 1 --max-time 5 "http://127.0.0.1:%EA_FUNNEL_ORIGIN_PORT%/api/health/ready" > "%WORK%\ready.json" 2>>"%LOG%" || exit /b 1
findstr /l ready "%WORK%\ready.json" >nul || exit /b 1
curl.exe --fail --silent --show-error --max-time 5 "http://127.0.0.1:%EA_FUNNEL_ORIGIN_PORT%/login" > "%WORK%\login.html" 2>>"%LOG%" || exit /b 1
findstr /l /c:"id=\"app\"" "%WORK%\login.html" >nul
exit /b %errorlevel%

:verify_image
docker --context desktop-linux image inspect --format "{{.Id}}" "ea-windows-rehearsal-%~1:%SUFFIX%" > "%WORK%\image.id" 2>>"%LOG%" || exit /b 1
findstr /x /l /g:"%~1.id" "%WORK%\image.id" >nul
exit /b %errorlevel%
