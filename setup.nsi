; Установщик InstallerModels. Ставит для текущего пользователя, без прав администратора.
; Собирается из build.py, вручную: makensis /DVERSION=1.0.0 setup.nsi

Unicode true

!ifndef VERSION
  !define VERSION "1.0.0"
!endif

!define APP     "InstallerModels"
!define PUBLISH "ALEXaloysha"
!define REGKEY  "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APP}"

!include "MUI2.nsh"
!include "FileFunc.nsh"

Name "${APP} ${VERSION}"
OutFile "dist\${APP}-Setup-${VERSION}.exe"
InstallDir "$LOCALAPPDATA\Programs\${APP}"
InstallDirRegKey HKCU "Software\${APP}" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUnInstDetails show

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "${APP}"
VIAddVersionKey "FileDescription" "Установщик моделей для ComfyUI"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "${PUBLISH}"

!define MUI_ICON "icon.ico"
!define MUI_UNICON "icon.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\${APP}.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить ${APP}"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

; Ищем окно программы по заголовку. Это надёжнее проверки блокировки файла:
; работает и в деинсталляторе, который запускается копией из %TEMP% и видит
; другой $INSTDIR. Заголовок должен совпадать с window.title() в gui.py.
!define WINTITLE "InstallerModels - модели для ComfyUI"

!macro RunningCheck un
Function ${un}CheckNotRunning
  again:
    FindWindow $R0 "" "${WINTITLE}"
    IntCmp $R0 0 free
    IfSilent 0 ask
      Abort "${APP} запущен, закрой программу и повтори."
    ask:
    MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "${APP} сейчас запущен. Закрой окно программы и нажми «Повтор»." IDRETRY again
    Abort "${APP} запущен, операция отменена."
  free:
FunctionEnd
!macroend

!insertmacro RunningCheck ""
!insertmacro RunningCheck "un."

Section "Программа" SecMain
  SectionIn RO
  Call CheckNotRunning
  SetOutPath "$INSTDIR"
  File /r "dist\app\${APP}\*.*"

  WriteRegStr HKCU "Software\${APP}" "InstallDir" "$INSTDIR"
  WriteUninstaller "$INSTDIR\Uninstall.exe"

  CreateDirectory "$SMPROGRAMS\${APP}"
  CreateShortcut "$SMPROGRAMS\${APP}\${APP}.lnk" "$INSTDIR\${APP}.exe"
  CreateShortcut "$SMPROGRAMS\${APP}\Удалить ${APP}.lnk" "$INSTDIR\Uninstall.exe"

  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  WriteRegStr   HKCU "${REGKEY}" "DisplayName"     "${APP}"
  WriteRegStr   HKCU "${REGKEY}" "DisplayVersion"  "${VERSION}"
  WriteRegStr   HKCU "${REGKEY}" "Publisher"       "${PUBLISH}"
  WriteRegStr   HKCU "${REGKEY}" "DisplayIcon"     "$INSTDIR\${APP}.exe"
  WriteRegStr   HKCU "${REGKEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr   HKCU "${REGKEY}" "UninstallString" "$\"$INSTDIR\Uninstall.exe$\""
  WriteRegDWORD HKCU "${REGKEY}" "EstimatedSize"   "$0"
  WriteRegDWORD HKCU "${REGKEY}" "NoModify"        1
  WriteRegDWORD HKCU "${REGKEY}" "NoRepair"        1
SectionEnd

Section "Ярлык на рабочем столе" SecDesktop
  CreateShortcut "$DESKTOP\${APP}.lnk" "$INSTDIR\${APP}.exe"
SectionEnd

LangString DESC_SecMain    ${LANG_RUSSIAN} "Сама программа и список моделей."
LangString DESC_SecDesktop ${LANG_RUSSIAN} "Положить ярлык на рабочий стол."

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
  !insertmacro MUI_DESCRIPTION_TEXT ${SecMain}    $(DESC_SecMain)
  !insertmacro MUI_DESCRIPTION_TEXT ${SecDesktop} $(DESC_SecDesktop)
!insertmacro MUI_FUNCTION_DESCRIPTION_END

Section "Uninstall"
  Call un.CheckNotRunning

  Delete "$DESKTOP\${APP}.lnk"
  Delete "$SMPROGRAMS\${APP}\${APP}.lnk"
  Delete "$SMPROGRAMS\${APP}\Удалить ${APP}.lnk"
  RMDir  "$SMPROGRAMS\${APP}"

  RMDir /r "$INSTDIR\_internal"
  Delete "$INSTDIR\${APP}.exe"
  Delete "$INSTDIR\models.json"
  Delete "$INSTDIR\README.md"
  Delete "$INSTDIR\Uninstall.exe"
  RMDir "$INSTDIR"

  DeleteRegKey HKCU "${REGKEY}"
  DeleteRegKey HKCU "Software\${APP}"
SectionEnd
