// 01-diagnose-document.js - DIAGNOSE one document (read-only). Fill in the parameters, run the whole file.
// Use it when an insert may have gone wrong: it reports which shell runs the script, which cluster it is
// connected to, the folder's resource/content counts, and - for one document - the stored byte size of a
// binary field next to the size you expected.
//
// Parameters ------------------------------------------------------------------------------------------
var DB_NAME        = "TEAM_SHARING_ENCRYPTED_PROD";
var COLLECTION     = "resourceContent";                   // e.g. "resourceContent" or "resource"
var BINARY_FIELD   = "content";                           // "content" for resourceContent, "metadata" for resource
var DOC_ID         = ObjectId("000000000000000000000000"); // the document to inspect
var FOLDER_ID      = ObjectId("000000000000000000000000"); // the Team Sharing folder
var EXPECTED_BYTES = 0;                                   // from the export (python: len(base64.b64decode(...)))
// -----------------------------------------------------------------------------------------------------
var hello = db.getSiblingDB("admin").runCommand({ hello: 1 });
var coll = db.getSiblingDB(DB_NAME).getCollection(COLLECTION);
var found = coll.aggregate([
  { $match: { _id: DOC_ID } },
  { $project: { _id: 1, folderId: 1, updateId: 1, fieldType: { $type: "$" + BINARY_FIELD },
                fieldBytes: { $binarySize: "$" + BINARY_FIELD }, docBytes: { $bsonSize: "$$ROOT" } } }
]).toArray();
printjson({
  shell: (typeof version === "function") ? version() : "unknown",
  replicaSet: hello.setName || "standalone",
  primary: hello.isWritablePrimary,
  database: DB_NAME,
  resourcesInFolder: db.getSiblingDB(DB_NAME).getCollection("resource").countDocuments({ folderId: FOLDER_ID }),
  contentsInFolder: db.getSiblingDB(DB_NAME).getCollection("resourceContent").countDocuments({ folderId: FOLDER_ID }),
  document: found.length ? found[0] : "NOT PRESENT",
  expectedBytes: EXPECTED_BYTES,
  verdict: !found.length ? "document is not present"
         : (found[0].fieldBytes === EXPECTED_BYTES ? "OK - stored size matches the export"
                                                   : "BROKEN - stored " + found[0].fieldBytes + " bytes, expected " + EXPECTED_BYTES)
});
