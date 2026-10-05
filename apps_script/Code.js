/**
 * Google Apps Script для Request Classifier.
 *
 * Приймає POST-запит із рядками результатів і додає їх у таблицю, до якої
 * прив'язано цей скрипт. Дозволяє записувати в Google Sheets без Google Cloud
 * та ключів сервісного акаунта.
 *
 * Розгортання:
 *   1. Таблиця -> Розширення -> Apps Script -> вставити цей код.
 *   2. Замінити TOKEN на довгий випадковий рядок (той самий піде у змінну
 *      GOOGLE_APPS_SCRIPT_TOKEN).
 *   3. Deploy -> New deployment -> тип "Web app":
 *        Execute as: Me
 *        Who has access: Anyone
 *   4. Скопіювати URL веб-застосунку (закінчується на /exec) у змінну
 *      GOOGLE_APPS_SCRIPT_URL.
 *
 * Після будь-якої зміни коду: Deploy -> Manage deployments -> Edit ->
 * Version: New version -> Deploy (інакше працюватиме стара версія).
 */

const TOKEN = 'ЗАМІНИ_НА_ДОВГИЙ_ВИПАДКОВИЙ_РЯДОК';

function doPost(e) {
  const lock = LockService.getScriptLock();
  let locked = false;
  try {
    const data = JSON.parse(e.postData.contents);
    if (data.token !== TOKEN) {
      return reply_({ ok: false, error: 'unauthorized' });
    }

    const rows = data.rows || [];
    if (rows.length === 0) {
      return reply_({ ok: true, written: 0 });
    }

    // Блокування, щоб одночасні запити не записались в одні й ті самі рядки.
    lock.waitLock(20000);
    locked = true;

    const spreadsheet = SpreadsheetApp.getActiveSpreadsheet();
    const sheet = data.tab
      ? spreadsheet.getSheetByName(data.tab) || spreadsheet.insertSheet(data.tab)
      : spreadsheet.getSheets()[0];

    if (sheet.getLastRow() === 0 && data.header) {
      sheet
        .getRange(1, 1, 1, data.header.length)
        .setNumberFormat('@')
        .setValues([data.header])
        .setFontWeight('bold');
      sheet.setFrozenRows(1);
    }

    // Формат "звичайний текст" ('@') не дає значенням, що починаються з "=",
    // виконуватись як формули.
    const start = sheet.getLastRow() + 1;
    sheet.getRange(start, 1, rows.length, rows[0].length).setNumberFormat('@').setValues(rows);

    return reply_({ ok: true, written: rows.length });
  } catch (err) {
    return reply_({ ok: false, error: String(err) });
  } finally {
    if (locked) {
      lock.releaseLock();
    }
  }
}

function reply_(payload) {
  return ContentService.createTextOutput(JSON.stringify(payload)).setMimeType(
    ContentService.MimeType.JSON
  );
}
