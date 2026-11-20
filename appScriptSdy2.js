var PDF_FOLDER_ID = "1_OzECEun2PKr0G6akk8fOjF-pJw4LtN2"; 

var ALLOWED_APPEND_TABS = {
  "Shift_Data": true,
  "Hourly_Data": true,
  "Reject_Data": true,
  "Downtime_Data": true,
  "Parameter_Data": true
};

function jsonOutput(body) {
  return ContentService
    .createTextOutput(JSON.stringify(body))
    .setMimeType(ContentService.MimeType.JSON);
}

function getRequestIdColumn(sheet, rowDataLength) {
  var lastColumn = Math.max(sheet.getLastColumn(), rowDataLength);
  if (lastColumn > 0) {
    var headers = sheet.getRange(1, 1, 1, lastColumn).getValues()[0];
    for (var index = 0; index < headers.length; index++) {
      if (cleanText(headers[index], "").toLowerCase() === "request_id") {
        return index + 1;
      }
    }
  }

  var requestColumn = rowDataLength + 1;
  var candidateHeader = cleanText(sheet.getRange(1, requestColumn).getValue(), "");
  if (candidateHeader !== "" && candidateHeader.toLowerCase() !== "request_id") {
    requestColumn = lastColumn + 1;
  }
  sheet.getRange(1, requestColumn).setValue("Request_ID");
  return requestColumn;
}

function sheetContainsRequestId(sheet, requestColumn, requestId) {
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) {
    return false;
  }

  var requestIds = sheet.getRange(2, requestColumn, lastRow - 1, 1).getValues();
  for (var index = 0; index < requestIds.length; index++) {
    if (cleanText(requestIds[index][0], "") === requestId) {
      return true;
    }
  }
  return false;
}

function appendRowIdempotently(sheet, rowData, requestId) {
  if (!requestId) {
    sheet.appendRow(rowData);
    return { appended: true, deduplicated: false };
  }

  var requestColumn = getRequestIdColumn(sheet, rowData.length);
  if (sheetContainsRequestId(sheet, requestColumn, requestId)) {
    return { appended: false, deduplicated: true };
  }

  var storedRow = rowData.slice();
  while (storedRow.length < requestColumn) {
    storedRow.push("");
  }
  storedRow[requestColumn - 1] = requestId;
  sheet.appendRow(storedRow);
  return { appended: true, deduplicated: false };
}

function driveQueryText(value) {
  return cleanText(value, "").replace(/'/g, "\\'");
}

function pdfPreviewUrl(file) {
  file.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
  return "https://drive.google.com/file/d/" + file.getId() + "/preview";
}

function findLatestPdfFileForShift(shiftId) {
  var folder = DriveApp.getFolderById(PDF_FOLDER_ID);
  var safeShiftId = driveQueryText(shiftId);
  var files = folder.searchFiles(
    "title contains '" + safeShiftId + "' and mimeType = 'application/pdf'"
  );
  var expectedPrefix = "PRS_" + cleanText(shiftId, "") + "_";
  var stableName = "PRS_" + cleanText(shiftId, "") + ".pdf";
  var latestFile = null;
  var latestDate = 0;

  while (files.hasNext()) {
    var currentFile = files.next();
    var currentName = currentFile.getName();
    if (currentName !== stableName && currentName.indexOf(expectedPrefix) !== 0) {
      continue;
    }
    var fileDate = currentFile.getDateCreated().getTime();
    if (fileDate > latestDate) {
      latestDate = fileDate;
      latestFile = currentFile;
    }
  }

  return latestFile;
}

function doPost(e) {
  var lock = LockService.getScriptLock();
  var lockAcquired = false;

  try {
    lock.waitLock(10000);
    lockAcquired = true;

    if (!e || !e.postData || !e.postData.contents) {
      throw new Error("Missing JSON request body.");
    }

    var payload = JSON.parse(e.postData.contents);
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
      throw new Error("The request body must be a JSON object.");
    }

    var action = cleanText(payload.action, "");
    if (!action) {
      throw new Error("Missing action.");
    }

    var requestId = cleanText(payload.request_id, "");
    if (requestId && !/^[A-Za-z0-9._:-]{1,100}$/.test(requestId)) {
      throw new Error("Invalid request_id.");
    }

    var responseCache = CacheService.getScriptCache();
    var cacheKey = requestId ? "google-request-" + requestId : "";
    if (cacheKey) {
      var cachedResponse = responseCache.get(cacheKey);
      if (cachedResponse) {
        return ContentService
          .createTextOutput(cachedResponse)
          .setMimeType(ContentService.MimeType.JSON);
      }
    }

    var responseBody;

    if (action === "APPEND_ROW") {
      var tabName = cleanText(payload.tab_name, "");
      if (!Object.prototype.hasOwnProperty.call(ALLOWED_APPEND_TABS, tabName)) {
        throw new Error("Unsupported sheet tab: " + tabName);
      }
      if (!Array.isArray(payload.row_data) || payload.row_data.length === 0) {
        throw new Error("row_data must be a non-empty array.");
      }

      var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(tabName);
      if (!sheet) {
        throw new Error("Could not find '" + tabName + "' tab.");
      }

      var appendResult = appendRowIdempotently(sheet, payload.row_data, requestId);
      SpreadsheetApp.flush();
      responseBody = {
        "status": "success",
        "message": appendResult.deduplicated ? "Duplicate request ignored." : "Row appended.",
        "tab_name": tabName,
        "deduplicated": appendResult.deduplicated,
        "request_id": requestId || null
      };
    } else if (action === "GENERATE_PDF") {
      var shiftId = cleanText(payload.shift_id, "");
      if (!shiftId) {
        throw new Error("Missing shift_id.");
      }

      var forceRegenerate = payload.force_regenerate === true;
      var existingPdf = forceRegenerate ? null : findLatestPdfFileForShift(shiftId);
      var pdfUrl = existingPdf ? pdfPreviewUrl(existingPdf) : generateShiftPDF(shiftId);
      responseBody = {
        "status": "success",
        "message": existingPdf ? "Existing PDF returned." : "PDF generated.",
        "url": pdfUrl,
        "deduplicated": Boolean(existingPdf),
        "request_id": requestId || null
      };
    } else if (action === "GET_SHIFT_LIST") {
      var folder = DriveApp.getFolderById(PDF_FOLDER_ID);
      var files = folder.searchFiles("mimeType = 'application/pdf'");
      var shiftSet = {};

      while (files.hasNext()) {
        var file = files.next();
        var parts = file.getName().split("_");
        if (parts.length >= 2 && parts[0] === "PRS") {
          shiftSet[parts[1]] = true;
        }
      }

      responseBody = {
        "status": "success",
        "shift_list": Object.keys(shiftSet).sort().reverse()
      };
    } else if (action === "GET_PDF_URL") {
      var requestedShiftId = cleanText(payload.shift_id, "");
      if (!requestedShiftId) {
        throw new Error("Missing shift_id.");
      }

      var latestFile = findLatestPdfFileForShift(requestedShiftId);

      if (!latestFile) {
        responseBody = {
          "status": "error",
          "message": "No PDF found for this shift yet."
        };
      } else {
        responseBody = {
          "status": "success",
          "url": pdfPreviewUrl(latestFile)
        };
      }
    } else {
      throw new Error("Unsupported action: " + action);
    }

    var responseJson = JSON.stringify(responseBody);
    if (cacheKey && responseBody.status === "success") {
      responseCache.put(cacheKey, responseJson, 21600);
    }
    return ContentService
      .createTextOutput(responseJson)
      .setMimeType(ContentService.MimeType.JSON);
  } catch (error) {
    return jsonOutput({
      "status": "error",
      "message": error && error.message ? error.message : String(error)
    });
  } finally {
    if (lockAcquired) {
      lock.releaseLock();
    }
  }
}

function isBlank(value) {
  return value === "" || value === null || typeof value === "undefined";
}

function cleanText(value, fallback) {
  if (isBlank(value) || String(value).trim() === "") {
    return typeof fallback === "undefined" ? "" : fallback;
  }
  return String(value).trim();
}

function toNumber(value) {
  if (isBlank(value)) {
    return 0;
  }
  var numberValue = Number(String(value).replace(/,/g, ""));
  return isNaN(numberValue) ? 0 : numberValue;
}

function roundToTwo(value) {
  return Math.round((toNumber(value) + Number.EPSILON) * 100) / 100;
}

function sameShiftId(value, shiftId) {
  return cleanText(value, "") === cleanText(shiftId, "");
}

function parseHourSlotStart(hourSlot) {
  var match = cleanText(hourSlot, "").match(/^(\d{1,2})(?:[.:]\d{1,2})?/);
  if (!match) {
    return null;
  }

  var hour = parseInt(match[1], 10);
  if (hour === 24) {
    hour = 0;
  }
  return hour >= 0 && hour <= 23 ? hour : null;
}

function parseWorkingTimeStartHour(workingTime, shiftType) {
  var text = cleanText(workingTime, "");
  var twelveHourMatch = text.match(/(\d{1,2})(?::(\d{2}))?\s*(AM|PM)/i);

  if (twelveHourMatch) {
    var twelveHour = parseInt(twelveHourMatch[1], 10) % 12;
    if (twelveHourMatch[3].toUpperCase() === "PM") {
      twelveHour += 12;
    }
    return twelveHour;
  }

  var twentyFourHourMatch = text.match(/^\s*(\d{1,2})(?::\d{2})?/);
  if (twentyFourHourMatch) {
    var twentyFourHour = parseInt(twentyFourHourMatch[1], 10);
    if (twentyFourHour >= 0 && twentyFourHour <= 23) {
      return twentyFourHour;
    }
  }

  return cleanText(shiftType, "").toLowerCase().indexOf("night") !== -1 ? 20 : 8;
}

function buildShiftRowMap(startHour) {
  var rowMap = {};
  for (var index = 0; index < 12; index++) {
    rowMap[String((startHour + index) % 24)] = 10 + (index * 2);
  }
  return rowMap;
}

function displayHour(hour) {
  var twelveHour = hour % 12;
  return twelveHour === 0 ? 12 : twelveHour;
}

function configureTemplateHourLabels(sheet, startHour) {
  for (var index = 0; index < 12; index++) {
    var hour = (startHour + index) % 24;
    var nextHour = (hour + 1) % 24;
    var rowNumber = 10 + (index * 2);
    sheet.getRange("D" + rowNumber).setValue(
      displayHour(hour) + ".00 - " + displayHour(nextHour) + ".00"
    );
  }
}

function findLatestShiftRow(data, shiftId) {
  for (var index = data.length - 1; index >= 1; index--) {
    if (sameShiftId(data[index][0], shiftId)) {
      return data[index];
    }
  }
  return null;
}

function appendUnique(values, value) {
  var text = cleanText(value, "");
  if (text !== "" && values.indexOf(text) === -1) {
    values.push(text);
  }
}

function dataRowFingerprint(row, fieldCount) {
  var values = [];
  for (var index = 0; index < fieldCount; index++) {
    var value = row[index];
    if (value instanceof Date) {
      values.push("date:" + value.getTime());
    } else {
      values.push(typeof value + ":" + cleanText(value, ""));
    }
  }
  return JSON.stringify(values);
}

function isRepeatedDataRow(seenRows, row, fieldCount) {
  var requestId = cleanText(row[fieldCount], "");
  var fingerprint = requestId !== ""
    ? "request:" + requestId
    : "legacy:" + dataRowFingerprint(row, fieldCount);
  if (Object.prototype.hasOwnProperty.call(seenRows, fingerprint)) {
    return true;
  }
  seenRows[fingerprint] = true;
  return false;
}

function aggregateHourlyData(data, shiftId, rowMap) {
  var hours = {};
  var modelTotals = {};
  var seenRows = {};

  for (var index = 1; index < data.length; index++) {
    var row = data[index];
    if (!sameShiftId(row[0], shiftId)) {
      continue;
    }
    if (isRepeatedDataRow(seenRows, row, 8)) {
      continue;
    }

    var startHour = parseHourSlotStart(row[3]);
    var hourKey = startHour === null ? "" : String(startHour);
    if (hourKey === "" || !Object.prototype.hasOwnProperty.call(rowMap, hourKey)) {
      continue;
    }

    if (!hours[hourKey]) {
      hours[hourKey] = {
        plan: 0,
        actual: 0,
        restMinutes: 0,
        segments: {},
        segmentOrder: []
      };
    }

    var model = cleanText(row[2], "-");
    var lotNumber = cleanText(row[6], "-");
    var plan = toNumber(row[4]);
    var actual = toNumber(row[5]);
    var restMinutes = toNumber(row[7]);
    var segmentKey = model + "\u001f" + lotNumber;
    var hourData = hours[hourKey];

    if (!hourData.segments[segmentKey]) {
      hourData.segments[segmentKey] = {
        model: model,
        lotNumber: lotNumber,
        plan: 0,
        actual: 0
      };
      hourData.segmentOrder.push(segmentKey);
    }

    hourData.plan += plan;
    hourData.actual += actual;
    hourData.restMinutes += restMinutes;
    hourData.segments[segmentKey].plan += plan;
    hourData.segments[segmentKey].actual += actual;

    if (model !== "-") {
      if (!Object.prototype.hasOwnProperty.call(modelTotals, model)) {
        modelTotals[model] = 0;
      }
      modelTotals[model] += actual;
    }
  }

  return {
    hours: hours,
    modelTotals: modelTotals
  };
}

function compactNumber(value) {
  return String(roundToTwo(value));
}

function getHourSegments(hourData) {
  var segments = [];
  for (var index = 0; index < hourData.segmentOrder.length; index++) {
    segments.push(hourData.segments[hourData.segmentOrder[index]]);
  }
  return segments;
}

function splitProductionHourRows(sheet, rowNumber) {
  sheet.getRange("B" + rowNumber + ":C" + (rowNumber + 1)).breakApart();
  sheet.getRange("B" + rowNumber + ":C" + rowNumber).merge();
  sheet.getRange("B" + (rowNumber + 1) + ":C" + (rowNumber + 1)).merge();
  sheet.getRange("E" + rowNumber + ":E" + (rowNumber + 1)).breakApart();
  sheet.getRange("F" + rowNumber + ":F" + (rowNumber + 1)).breakApart();
  sheet.getRange("G" + rowNumber + ":G" + (rowNumber + 1)).breakApart();
}

function writeProductionSegment(sheet, rowNumber, segments) {
  var modelValues = [];
  var planValues = [];
  var actualValues = [];
  var lotValues = [];

  for (var index = 0; index < segments.length; index++) {
    modelValues.push(segments[index].model);
    planValues.push(compactNumber(segments[index].plan));
    actualValues.push(compactNumber(segments[index].actual));
    lotValues.push(segments[index].lotNumber);
  }

  var modelCell = sheet.getRange("B" + rowNumber);
  var planCell = sheet.getRange("E" + rowNumber);
  var actualCell = sheet.getRange("F" + rowNumber);
  var lotCell = sheet.getRange("G" + rowNumber);

  modelCell.setValue(modelValues.join("\n")).setWrap(true);
  planCell.setValue(planValues.join("\n")).setWrap(true);
  actualCell.setValue(actualValues.join("\n")).setWrap(true);
  lotCell.setValue(lotValues.join("\n")).setWrap(true);

  var fontSize = segments.length > 1 ? 14 : 18;
  modelCell.setFontSize(fontSize);
  planCell.setFontSize(fontSize).setFontWeight("bold");
  actualCell.setFontSize(fontSize).setFontWeight("bold");
  lotCell.setFontSize(fontSize);
}

function writeHourlyData(sheet, data, shiftId, rowMap) {
  var aggregate = aggregateHourlyData(data, shiftId, rowMap);

  for (var hourKey in aggregate.hours) {
    if (!Object.prototype.hasOwnProperty.call(aggregate.hours, hourKey)) {
      continue;
    }

    var hourData = aggregate.hours[hourKey];
    var rowNumber = rowMap[hourKey];
    var segments = getHourSegments(hourData);

    if (segments.length === 1) {
      writeProductionSegment(sheet, rowNumber, segments);
    } else {
      splitProductionHourRows(sheet, rowNumber);
      writeProductionSegment(sheet, rowNumber, [segments[0]]);
      writeProductionSegment(sheet, rowNumber + 1, segments.slice(1));
    }

    sheet.getRange("D" + (rowNumber + 1)).setValue(
      hourData.restMinutes > 0 ? roundToTwo(hourData.restMinutes) : ""
    ).setFontSize(16);
  }

  return aggregate.modelTotals;
}

function aggregateRejectData(data, shiftId, rowMap) {
  var hours = {};
  var seenRows = {};

  for (var index = 1; index < data.length; index++) {
    var row = data[index];
    if (!sameShiftId(row[0], shiftId)) {
      continue;
    }
    if (isRepeatedDataRow(seenRows, row, 10)) {
      continue;
    }

    var startHour = parseHourSlotStart(row[1]);
    var hourKey = startHour === null ? "" : String(startHour);
    if (hourKey === "" || !Object.prototype.hasOwnProperty.call(rowMap, hourKey)) {
      continue;
    }

    if (!hours[hourKey]) {
      hours[hourKey] = {
        slab: 0,
        slabCodes: [],
        returnRoll: 0,
        ohtNumbers: [],
        rejectNg: 0,
        ngCodes: [],
        loft: 0,
        loftCodes: []
      };
    }

    var hourData = hours[hourKey];
    hourData.slab += toNumber(row[2]);
    appendUnique(hourData.slabCodes, row[3]);
    hourData.returnRoll += toNumber(row[4]);
    appendUnique(hourData.ohtNumbers, row[5]);
    hourData.rejectNg += toNumber(row[6]);
    appendUnique(hourData.ngCodes, row[7]);
    hourData.loft += toNumber(row[8]);
    appendUnique(hourData.loftCodes, row[9]);
  }

  return hours;
}

function joinedOrDash(values) {
  return values.length > 0 ? values.join(", ") : "-";
}

function writeRejectData(sheet, data, shiftId, rowMap) {
  var hours = aggregateRejectData(data, shiftId, rowMap);

  for (var hourKey in hours) {
    if (!Object.prototype.hasOwnProperty.call(hours, hourKey)) {
      continue;
    }

    var rowNumber = rowMap[hourKey];
    var hourData = hours[hourKey];
    sheet.getRange("N" + rowNumber).setValue(roundToTwo(hourData.slab)).setFontSize(16);
    sheet.getRange("O" + rowNumber).setValue(joinedOrDash(hourData.slabCodes)).setWrap(true).setFontSize(16);
    sheet.getRange("P" + rowNumber).setValue(roundToTwo(hourData.returnRoll)).setFontSize(16);
    sheet.getRange("Q" + rowNumber).setValue(joinedOrDash(hourData.ohtNumbers)).setWrap(true).setFontSize(16);
    sheet.getRange("R" + rowNumber).setValue(roundToTwo(hourData.rejectNg)).setFontSize(16);
    sheet.getRange("S" + rowNumber).setValue(joinedOrDash(hourData.ngCodes)).setWrap(true).setFontSize(16);
    sheet.getRange("T" + rowNumber).setValue(roundToTwo(hourData.loft)).setFontSize(16);
    sheet.getRange("U" + rowNumber).setValue(joinedOrDash(hourData.loftCodes)).setWrap(true).setFontSize(16);
  }
}

function downtimeBucket(category) {
  var normalized = cleanText(category, "").toLowerCase();
  if (normalized.indexOf("schedule") !== -1 || normalized.indexOf("planned") !== -1) {
    return "schedule";
  }
  if (
    normalized.indexOf("machine") !== -1 ||
    normalized.indexOf("mechanical") !== -1 ||
    normalized.indexOf("electrical") !== -1 ||
    normalized.indexOf("facility") !== -1 ||
    normalized.indexOf("maintenance") !== -1
  ) {
    return "machine";
  }
  return "production";
}

function newDowntimeBucket() {
  return {
    codes: [],
    duration: 0,
    hasDuration: false
  };
}

function aggregateDowntimeData(data, shiftId, rowMap) {
  var hours = {};
  var seenRows = {};

  for (var index = 1; index < data.length; index++) {
    var row = data[index];
    if (!sameShiftId(row[0], shiftId)) {
      continue;
    }
    if (isRepeatedDataRow(seenRows, row, 7)) {
      continue;
    }

    var startHour = parseHourSlotStart(row[1]);
    var hourKey = startHour === null ? "" : String(startHour);
    if (hourKey === "" || !Object.prototype.hasOwnProperty.call(rowMap, hourKey)) {
      continue;
    }

    if (!hours[hourKey]) {
      hours[hourKey] = {
        schedule: newDowntimeBucket(),
        production: newDowntimeBucket(),
        machine: newDowntimeBucket(),
        remarks: []
      };
    }

    var hourData = hours[hourKey];
    var bucketName = downtimeBucket(row[2]);
    var bucket = hourData[bucketName];
    var code = cleanText(row[3], "");
    var description = cleanText(row[5], "");
    var remarks = cleanText(row[6], "");

    appendUnique(bucket.codes, code);
    if (!isBlank(row[4]) && String(row[4]).trim() !== "") {
      bucket.duration += toNumber(row[4]);
      bucket.hasDuration = true;
    }

    var detailParts = [];
    if (code !== "") {
      detailParts.push(code);
    }
    if (description !== "") {
      detailParts.push(description);
    }
    var detail = detailParts.join(": ");
    if (remarks !== "") {
      detail += (detail === "" ? "" : " - ") + remarks;
    }
    appendUnique(hourData.remarks, detail);
  }

  return hours;
}

function writeDowntimeBucket(sheet, rowNumber, bucket, codeColumn, minuteColumn) {
  if (bucket.codes.length === 0 && !bucket.hasDuration) {
    return;
  }
  sheet.getRange(codeColumn + rowNumber)
    .setValue(joinedOrDash(bucket.codes))
    .setWrap(true)
    .setFontSize(16);
  sheet.getRange(minuteColumn + rowNumber).setValue(
    bucket.hasDuration ? roundToTwo(bucket.duration) : "-"
  ).setFontSize(16);
}

function writeDowntimeData(sheet, data, shiftId, rowMap) {
  var hours = aggregateDowntimeData(data, shiftId, rowMap);

  for (var hourKey in hours) {
    if (!Object.prototype.hasOwnProperty.call(hours, hourKey)) {
      continue;
    }

    var rowNumber = rowMap[hourKey];
    var hourData = hours[hourKey];
    writeDowntimeBucket(sheet, rowNumber, hourData.schedule, "AA", "AB");
    writeDowntimeBucket(sheet, rowNumber, hourData.production, "AC", "AD");
    writeDowntimeBucket(sheet, rowNumber, hourData.machine, "AE", "AF");

    if (hourData.remarks.length > 0) {
      sheet.getRange("AA" + (rowNumber + 1))
        .setValue(hourData.remarks.join(" | "))
        .setWrap(true)
        .setFontSize(14);
    }
  }
}

function writeSummaryData(sheet, modelTotals) {
  var summaryColumns = ["AD", "AE", "AF", "AG"];
  var models = Object.keys(modelTotals);
  var normalCount = models.length > 4 ? 3 : models.length;

  for (var index = 0; index < normalCount; index++) {
    var column = summaryColumns[index];
    sheet.getRange(column + "54").setValue(models[index]).setFontSize(16).setFontWeight("bold");
    sheet.getRange(column + "56").setValue(roundToTwo(modelTotals[models[index]])).setFontSize(18).setFontWeight("bold");
  }

  if (models.length > 4) {
    var overflowModels = models.slice(3);
    var overflowTotal = 0;
    for (var overflowIndex = 0; overflowIndex < overflowModels.length; overflowIndex++) {
      overflowTotal += modelTotals[overflowModels[overflowIndex]];
    }
    sheet.getRange("AG54").setValue(overflowModels.join("\n")).setWrap(true).setFontSize(14).setFontWeight("bold");
    sheet.getRange("AG56").setValue(roundToTwo(overflowTotal)).setFontSize(18).setFontWeight("bold");
  } else if (models.length === 4) {
    sheet.getRange("AG54").setValue(models[3]).setFontSize(16).setFontWeight("bold");
    sheet.getRange("AG56").setValue(roundToTwo(modelTotals[models[3]])).setFontSize(18).setFontWeight("bold");
  }
}

function findLatestParameterRows(data, shiftId) {
  var rowsByModel = {};
  var modelOrder = [];

  for (var index = 1; index < data.length; index++) {
    var row = data[index];
    if (!sameShiftId(row[0], shiftId)) {
      continue;
    }

    var model = cleanText(row[1], "-");
    if (!Object.prototype.hasOwnProperty.call(rowsByModel, model)) {
      modelOrder.push(model);
    }
    rowsByModel[model] = row;
  }

  return {
    rowsByModel: rowsByModel,
    modelOrder: modelOrder
  };
}

function writeParameterData(sheet, data, shiftId) {
  var latest = findLatestParameterRows(data, shiftId);
  var tableOneRows = [41, 43, 45, 47];
  var tableTwoRows = [55, 57, 59];
  var tableThreeRows = [41, 45];
  var glueCount = 0;

  for (var index = 0; index < latest.modelOrder.length; index++) {
    var model = latest.modelOrder[index];
    var row = latest.rowsByModel[model];

    if (index < tableOneRows.length) {
      var tableOneRow = tableOneRows[index];
      sheet.getRange("V" + tableOneRow).setValue(model).setFontSize(16).setFontWeight("bold");
      sheet.getRange("W" + tableOneRow).setValue(row[2]).setFontSize(16);
      sheet.getRange("X" + tableOneRow).setValue(row[3]).setFontSize(16);
      sheet.getRange("Y" + tableOneRow).setValue(row[4]).setFontSize(16);
      sheet.getRange("Z" + tableOneRow).setValue(row[5]).setFontSize(16);
    }

    if (index < tableTwoRows.length) {
      var tableTwoRow = tableTwoRows[index];
      sheet.getRange("V" + tableTwoRow).setValue(model).setFontSize(16).setFontWeight("bold");
      sheet.getRange("W" + tableTwoRow).setValue(row[6]).setFontSize(16);
      sheet.getRange("X" + tableTwoRow).setValue(row[7]).setFontSize(16);
      sheet.getRange("Y" + tableTwoRow).setValue(row[8]).setFontSize(16);
    }

    var glueStandard = row[9];
    var glueActual = row[10];
    var hasGlue = (
      (!isBlank(glueStandard) && cleanText(glueStandard, "") !== "-") ||
      (!isBlank(glueActual) && cleanText(glueActual, "") !== "-")
    );

    if (hasGlue && glueCount < tableThreeRows.length) {
      var tableThreeRow = tableThreeRows[glueCount];
      sheet.getRange("AD" + tableThreeRow).setValue(model).setFontSize(16).setFontWeight("bold");
      sheet.getRange("AE" + tableThreeRow).setValue(glueStandard).setFontSize(16);
      sheet.getRange("AF" + tableThreeRow).setValue(glueActual).setFontSize(16);
      glueCount++;
    }
  }
}

function generateShiftPDF(shiftId) {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  
  var templateSheet = ss.getSheetByName("PRS_Template");
  if (!templateSheet) {
    throw new Error("Could not find 'PRS_Template' tab.");
  }
  var shiftDataSheet = ss.getSheetByName("Shift_Data");
  if (!shiftDataSheet) {
    throw new Error("Could not find 'Shift_Data' tab.");
  }

  var shiftData = shiftDataSheet.getDataRange().getValues();
  var targetShift = findLatestShiftRow(shiftData, shiftId);
  if (!targetShift) {
    throw new Error("Shift ID '" + shiftId + "' was not found in Shift_Data.");
  }
  
  var uniqueTempName = "TEMP_" + shiftId + "_" + new Date().getTime();
  var tempSheet = templateSheet.copyTo(ss);
  tempSheet.setName(uniqueTempName);
  
  try {
    tempSheet.getRange("C3").setValue(targetShift[2]).setFontSize(18).setFontWeight("bold"); // Line
    tempSheet.getRange("G3").setValue(targetShift[3]).setFontSize(18).setFontWeight("bold"); // Shift
    tempSheet.getRange("C4").setValue(targetShift[1]).setFontSize(18).setFontWeight("bold"); // Production date
    tempSheet.getRange("H4").setValue(targetShift[5]).setFontSize(18).setFontWeight("bold"); // Working time
    tempSheet.getRange("N3").setValue(targetShift[6]).setFontSize(18).setFontWeight("bold"); // Supervisor
    tempSheet.getRange("N4").setValue(targetShift[7]).setFontSize(18).setFontWeight("bold"); // Leader
    tempSheet.getRange("V3").setValue(targetShift[8]).setFontSize(18).setFontWeight("bold"); // Forming operator
    tempSheet.getRange("V4").setValue(targetShift[9]).setFontSize(18).setFontWeight("bold"); // Waterjet operator
    tempSheet.getRange("AC3").setValue(targetShift[10]).setFontSize(18).setFontWeight("bold"); // Assembly operator
    tempSheet.getRange("AC4").setValue(targetShift[11]).setFontSize(18).setFontWeight("bold"); // Quality inspector

    var shiftType = cleanText(targetShift[3], "");
    var startHour = parseWorkingTimeStartHour(targetShift[5], shiftType);
    var rowMap = buildShiftRowMap(startHour);
    configureTemplateHourLabels(tempSheet, startHour);

    for (var rowIndex = 0; rowIndex < 12; rowIndex++) {
      var templateRow = 10 + (rowIndex * 2);
      tempSheet.getRange("N" + templateRow + ":U" + templateRow).setValue("-");
      tempSheet.getRange("AA" + templateRow + ":AF" + templateRow).setValue("-");
      tempSheet.getRange("AA" + (templateRow + 1)).setValue("-");
    }

    var modelTotals = {};
    var hourlySheet = ss.getSheetByName("Hourly_Data");
    if (hourlySheet) {
      modelTotals = writeHourlyData(
        tempSheet,
        hourlySheet.getDataRange().getValues(),
        shiftId,
        rowMap
      );
    }

    var rejectSheet = ss.getSheetByName("Reject_Data");
    if (rejectSheet) {
      writeRejectData(
        tempSheet,
        rejectSheet.getDataRange().getValues(),
        shiftId,
        rowMap
      );
    }

    var downtimeSheet = ss.getSheetByName("Downtime_Data");
    if (downtimeSheet) {
      writeDowntimeData(
        tempSheet,
        downtimeSheet.getDataRange().getValues(),
        shiftId,
        rowMap
      );
    }

    writeSummaryData(tempSheet, modelTotals);

    var parameterSheet = ss.getSheetByName("Parameter_Data");
    if (parameterSheet) {
      writeParameterData(
        tempSheet,
        parameterSheet.getDataRange().getValues(),
        shiftId
      );
    }
    
    SpreadsheetApp.flush();
    
    var folder = DriveApp.getFolderById(PDF_FOLDER_ID);
    var url = "https://docs.google.com/spreadsheets/d/" + ss.getId() +
      "/export?exportFormat=pdf&format=pdf&size=A3&portrait=false&scale=2" +
      "&sheetnames=false&printtitle=false&pagenumbers=false&gridlines=false&fzr=false" +
      "&horizontal_alignment=CENTER&vertical_alignment=TOP&range=A1%3AAJ62" +
      "&top_margin=0.10&bottom_margin=0.10&left_margin=0.10&right_margin=0.10" +
      "&gid=" + tempSheet.getSheetId();

    var token = ScriptApp.getOAuthToken();
    var response = UrlFetchApp.fetch(url, { headers: { 'Authorization': 'Bearer ' + token } });
    if (response.getResponseCode() !== 200) {
      throw new Error("Google Sheets PDF export failed with HTTP " + response.getResponseCode() + ".");
    }
    
    var pdfName = "PRS_" + shiftId + "_" + Utilities.formatDate(new Date(), "GMT+8", "HHmmss") + ".pdf";
    var blob = response.getBlob().setName(pdfName);
    var newFile = folder.createFile(blob);
    
    // ==============================================================
    // NEW: Update file permissions and return the iframe preview link!
    // ==============================================================
    newFile.setSharing(DriveApp.Access.ANYONE_WITH_LINK, DriveApp.Permission.VIEW);
    var iframeUrl = "https://drive.google.com/file/d/" + newFile.getId() + "/preview";
    
    return iframeUrl;
    
  } finally {
    ss.deleteSheet(tempSheet);
  }
}

function testRun() {
  var testShiftId = "20260803-Day-Line2"; 
  Logger.log("Starting test for Shift ID: " + testShiftId);
  var resultUrl = generateShiftPDF(testShiftId);
  Logger.log("Test Complete! PDF URL: " + resultUrl);
}
