# Architecture overview

CLD2 packages a directory into a range-readable repository made of metadata, chunks and pack data. The design is optimized for update distribution experiments, not for generic compression competition.

Core concepts:

- **chunking:** fixed/CDC/FastCDC-style chunk boundaries;
- **object reuse:** unchanged chunks can be reused across releases;
- **range reads:** clients can fetch only needed pack ranges;
- **cache:** local chunk cache can reduce warm update transfer;
- **audit/repair:** installs can be audited and repaired;
- **cost-aware planner:** benchmark utilities estimate transfer and object-store cost trade-offs.

Important distinction:

- zsync works file/tar-oriented;
- CLD2 works repository/chunk/object-oriented.

This means comparisons are useful but not perfectly equivalent.
