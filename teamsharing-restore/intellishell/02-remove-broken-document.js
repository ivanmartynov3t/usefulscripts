// 02-remove-broken-document.js - remove ONE document that a failed restore step wrote incompletely.
// Only for a document that this restore inserted (the pre-flight proved it did not exist before) and that
// 01-diagnose-document.js reported as BROKEN. The script deletes nothing unless every check below holds:
//   * connected to the production replica set and CONFIRM is true
//   * the document exists, belongs to FOLDER_ID and (if given) carries UPDATE_ID
//   * its binary field is NOT the expected size (an intact document is never deleted)
//   * no resource references it (for resourceContent) - so nothing that is in use can go
//
// Parameters ------------------------------------------------------------------------------------------
var CONFIRM        = false;                                // <-- set to true to actually delete
var PROD_SET       = "atlas-blssko-shard-0";               // replica set name of the production cluster
var DB_NAME        = "TEAM_SHARING_ENCRYPTED_PROD";
var COLLECTION     = "resourceContent";
var BINARY_FIELD   = "content";
var DOC_ID         = ObjectId("000000000000000000000000");
var FOLDER_ID      = ObjectId("000000000000000000000000");
var UPDATE_ID      = "";                                   // optional: the document's updateId from the export
var EXPECTED_BYTES = 0;                                    // the intact size from the export
// -----------------------------------------------------------------------------------------------------
var ABORT = false;
function fail(m) { ABORT = true; print("!! " + m + "  -> nothing was deleted."); }
var hello = db.getSiblingDB("admin").runCommand({ hello: 1 });
if (hello.setName !== PROD_SET) fail("connected to '" + (hello.setName || "standalone") + "', expected production '" + PROD_SET + "'");
if (!CONFIRM) fail("CONFIRM is false (set it to true at the top of this script)");
var coll = db.getSiblingDB(DB_NAME).getCollection(COLLECTION);
var doc = coll.aggregate([{ $match: { _id: DOC_ID } },
  { $project: { folderId: 1, updateId: 1, fieldBytes: { $binarySize: "$" + BINARY_FIELD } } }]).toArray()[0];
if (!doc) fail("document " + DOC_ID + " is not present");
else {
  if (!doc.folderId || !doc.folderId.equals(FOLDER_ID)) fail("document does not belong to folder " + FOLDER_ID);
  if (UPDATE_ID && doc.updateId !== UPDATE_ID) fail("updateId '" + doc.updateId + "' does not match '" + UPDATE_ID + "'");
  if (doc.fieldBytes === EXPECTED_BYTES) fail("document looks intact (" + doc.fieldBytes + " bytes) - not deleting");
  if (COLLECTION === "resourceContent" && db.getSiblingDB(DB_NAME).getCollection("resource").countDocuments({ contentId: DOC_ID }) !== 0)
    fail("a resource references this content - not deleting");
}
if (!ABORT) {
  var filter = { _id: DOC_ID }; if (UPDATE_ID) filter.updateId = UPDATE_ID;
  var r = coll.deleteOne(filter);
  printjson({ deleted: r.deletedCount, storedBytesWere: doc.fieldBytes, expectedBytes: EXPECTED_BYTES,
              remainingInFolder: coll.countDocuments({ folderId: FOLDER_ID }) });
  print(r.deletedCount === 1 ? "OK - broken document removed; re-run the insert script, it will write it again correctly."
                             : "CHECK - nothing deleted.");
}
