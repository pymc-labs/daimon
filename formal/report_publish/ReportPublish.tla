-------------------------- MODULE ReportPublish --------------------------
EXTENDS Naturals, FiniteSets

CONSTANT Legacy

Stages == {"ready", "exposed", "archived", "accepted", "pdf-written",
           "failed", "committed", "crashed"}
PDFs == {"old.pdf", "new.pdf"}
Archives == {"old.tar.gz", "new.tar.gz"}
Bundles == {"old-bundle", "new-bundle"}
Digests == {"old-digest", "new-digest"}

VARIABLES stage, currentPDF, bundle, digest, archivePath, pdfFiles,
          revisions, archiveFiles, archiveExists, remoteAccepted
vars == <<stage, currentPDF, bundle, digest, archivePath, pdfFiles,
          revisions, archiveFiles, archiveExists, remoteAccepted>>

Init ==
    /\ stage = "ready"
    /\ currentPDF = "old.pdf"
    /\ bundle = "old-bundle"
    /\ digest = "old-digest"
    /\ archivePath = "old.tar.gz"
    /\ pdfFiles = {"old.pdf"}
    /\ revisions = {"old.pdf"}
    /\ archiveFiles = [a \in Archives |-> "old-bundle"]
    /\ archiveExists = {"old.tar.gz"}
    /\ remoteAccepted = FALSE

LegacyExpose ==
    /\ Legacy
    /\ stage = "ready"
    /\ stage' = "exposed"
    /\ currentPDF' = "new.pdf"
    /\ pdfFiles' = pdfFiles \cup {"new.pdf"}
    /\ revisions' = revisions \cup {"new.pdf"}
    /\ UNCHANGED <<bundle, digest, archivePath, archiveFiles, archiveExists, remoteAccepted>>

PersistUniqueArchive ==
    /\ ~Legacy
    /\ stage = "ready"
    /\ stage' = "archived"
    /\ archiveFiles' = [archiveFiles EXCEPT !["new.tar.gz"] = "new-bundle"]
    /\ archiveExists' = archiveExists \cup {"new.tar.gz"}
    /\ UNCHANGED <<currentPDF, bundle, digest, archivePath, pdfFiles, revisions, remoteAccepted>>

PersistOverCurrentArchive ==
    /\ Legacy
    /\ stage = "exposed"
    /\ stage' = "archived"
    /\ archiveFiles' = [archiveFiles EXCEPT !["old.tar.gz"] = "new-bundle"]
    /\ UNCHANGED <<currentPDF, bundle, digest, archivePath, pdfFiles, revisions, archiveExists, remoteAccepted>>

SeamAccept ==
    /\ stage = "archived"
    /\ stage' = "accepted"
    /\ remoteAccepted' = TRUE
    /\ UNCHANGED <<currentPDF, bundle, digest, archivePath, pdfFiles, revisions, archiveFiles, archiveExists>>

SeamReject ==
    /\ stage = "archived"
    /\ stage' = "failed"
    /\ UNCHANGED <<currentPDF, bundle, digest, archivePath, pdfFiles, revisions, archiveFiles,
                   archiveExists, remoteAccepted>>

SeamTimeout ==
    /\ stage = "archived"
    /\ stage' = "failed"
    /\ remoteAccepted' \in {TRUE, FALSE}
    /\ UNCHANGED <<currentPDF, bundle, digest, archivePath, pdfFiles, revisions, archiveFiles, archiveExists>>

WriteAcceptedPDF ==
    /\ ~Legacy
    /\ stage = "accepted"
    /\ stage' = "pdf-written"
    /\ pdfFiles' = pdfFiles \cup {"new.pdf"}
    /\ UNCHANGED <<currentPDF, bundle, digest, archivePath, revisions, archiveFiles, archiveExists,
                   remoteAccepted>>

CommitAcceptedPublish ==
    /\ ~Legacy
    /\ stage = "pdf-written"
    /\ remoteAccepted
    /\ stage' = "committed"
    /\ currentPDF' = "new.pdf"
    /\ bundle' = "new-bundle"
    /\ digest' = "new-digest"
    /\ archivePath' = "new.tar.gz"
    /\ revisions' = revisions \cup {"new.pdf"}
    /\ UNCHANGED <<pdfFiles, archiveFiles, archiveExists, remoteAccepted>>

CommitFailure ==
    /\ ~Legacy
    /\ stage = "pdf-written"
    /\ stage' = "failed"
    /\ UNCHANGED <<currentPDF, bundle, digest, archivePath, pdfFiles, revisions,
                   archiveFiles, archiveExists, remoteAccepted>>

LegacyCommitBundle ==
    /\ Legacy
    /\ stage = "accepted"
    /\ stage' = "committed"
    /\ bundle' = "new-bundle"
    /\ digest' = "new-digest"
    /\ archivePath' = "old.tar.gz"
    /\ revisions' = revisions
    /\ UNCHANGED <<currentPDF, pdfFiles, archiveFiles, archiveExists, remoteAccepted>>

Crash ==
    /\ stage \notin {"failed", "committed", "crashed"}
    /\ stage' = "crashed"
    /\ UNCHANGED <<currentPDF, bundle, digest, archivePath, pdfFiles,
                   revisions, archiveFiles, archiveExists, remoteAccepted>>

Stop ==
    /\ stage \in {"failed", "committed", "crashed"}
    /\ UNCHANGED vars

Next == LegacyExpose \/ PersistUniqueArchive \/ PersistOverCurrentArchive
        \/ SeamAccept \/ SeamReject \/ SeamTimeout \/ WriteAcceptedPDF
        \/ CommitAcceptedPublish \/ CommitFailure \/ LegacyCommitBundle \/ Crash \/ Stop

TypeOK ==
    /\ stage \in Stages
    /\ currentPDF \in PDFs
    /\ bundle \in Bundles
    /\ digest \in Digests
    /\ archivePath \in Archives
    /\ pdfFiles \subseteq PDFs
    /\ revisions \subseteq PDFs
    /\ archiveFiles \in [Archives -> Bundles]
    /\ archiveExists \subseteq Archives
    /\ remoteAccepted \in BOOLEAN

VisibleArtifactsCoherent ==
    /\ currentPDF \in pdfFiles
    /\ currentPDF \in revisions
    /\ (currentPDF = "old.pdf" =>
          /\ bundle = "old-bundle"
          /\ digest = "old-digest")
    /\ (currentPDF = "new.pdf" =>
          /\ bundle = "new-bundle"
          /\ digest = "new-digest"
          /\ remoteAccepted
          /\ archivePath = "new.tar.gz")
    /\ archivePath \in archiveExists
    /\ archiveFiles[archivePath] = bundle
    /\ (bundle = "old-bundle" => digest = "old-digest")
    /\ (bundle = "new-bundle" =>
          /\ digest = "new-digest"
          /\ remoteAccepted
          /\ currentPDF = "new.pdf"
          /\ archivePath = "new.tar.gz")

Spec == Init /\ [][Next]_vars

THEOREM Spec => []TypeOK
THEOREM Spec => []VisibleArtifactsCoherent
=============================================================================
