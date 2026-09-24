-------------------------- MODULE PublishOverlap --------------------------
EXTENDS Naturals, FiniteSets, TLC

CONSTANTS ProtectInFlight

Publishers == {"slow", "fast"}
Archives == {"old.tar.gz", "slow.tar.gz", "fast.tar.gz"}
PDFs == {"old.pdf", "slow.pdf", "fast.pdf"}
Bundles == {"old-bundle", "slow-bundle", "fast-bundle"}
Digests == {"old-digest", "slow-digest", "fast-digest"}
Phases == {"idle", "pushing", "accepted", "committed", "failed"}

ArchiveFor(p) == IF p = "slow" THEN "slow.tar.gz" ELSE "fast.tar.gz"
PDFFor(p) == IF p = "slow" THEN "slow.pdf" ELSE "fast.pdf"
BundleFor(p) == IF p = "slow" THEN "slow-bundle" ELSE "fast-bundle"
DigestFor(p) == IF p = "slow" THEN "slow-digest" ELSE "fast-digest"
CandidateArchives == {ArchiveFor(p) : p \in Publishers}

VARIABLES phase, activeArchives, agedArchives, archiveExists, archiveContents,
          currentPDF, bundle, digest, archivePath, pdfFiles, revisions,
          readerStarted, readerPDF, readerRevisions, readerFetched
vars == <<phase, activeArchives, agedArchives, archiveExists, archiveContents,
          currentPDF, bundle, digest, archivePath, pdfFiles, revisions,
          readerStarted, readerPDF, readerRevisions, readerFetched>>

Init ==
    /\ phase = [p \in Publishers |-> "idle"]
    /\ activeArchives = {}
    /\ agedArchives = {}
    /\ archiveExists = {"old.tar.gz"}
    /\ archiveContents = [a \in Archives |->
          IF a = "old.tar.gz" THEN "old-bundle" ELSE "none"]
    /\ currentPDF = "old.pdf"
    /\ bundle = "old-bundle"
    /\ digest = "old-digest"
    /\ archivePath = "old.tar.gz"
    /\ pdfFiles = {"old.pdf"}
    /\ revisions = {"old.pdf"}
    /\ readerStarted = FALSE
    /\ readerPDF = "old.pdf"
    /\ readerRevisions = {"old.pdf"}
    /\ readerFetched = FALSE

BeginPublish(p) ==
    /\ p \in Publishers
    /\ phase[p] = "idle"
    /\ LET prunable == {a \in archiveExists :
              a \in agedArchives
              /\ a # archivePath
              /\ (~ProtectInFlight \/ a \notin activeArchives)}
           archive == ArchiveFor(p)
       IN
          /\ phase' = [phase EXCEPT ![p] = "pushing"]
          /\ activeArchives' = activeArchives \cup {archive}
          /\ archiveExists' = (archiveExists \ prunable) \cup {archive}
          /\ archiveContents' = [archiveContents EXCEPT ![archive] = BundleFor(p)]
          /\ UNCHANGED <<agedArchives, currentPDF, bundle, digest, archivePath,
                         pdfFiles, revisions, readerStarted, readerPDF,
                         readerRevisions, readerFetched>>

LongPendingPush(p) ==
    /\ p \in Publishers
    /\ phase[p] = "pushing"
    /\ ArchiveFor(p) \in archiveExists
    /\ ArchiveFor(p) \notin agedArchives
    /\ agedArchives' = agedArchives \cup {ArchiveFor(p)}
    /\ UNCHANGED <<phase, activeArchives, archiveExists, archiveContents,
                   currentPDF, bundle, digest, archivePath, pdfFiles, revisions,
                   readerStarted, readerPDF, readerRevisions, readerFetched>>

SeamAccept(p) ==
    /\ p \in Publishers
    /\ phase[p] = "pushing"
    /\ phase' = [phase EXCEPT ![p] = "accepted"]
    /\ UNCHANGED <<activeArchives, agedArchives, archiveExists, archiveContents,
                   currentPDF, bundle, digest, archivePath, pdfFiles, revisions,
                   readerStarted, readerPDF, readerRevisions, readerFetched>>

PublishReject(p) ==
    /\ p \in Publishers
    /\ phase[p] = "pushing"
    /\ phase' = [phase EXCEPT ![p] = "failed"]
    /\ activeArchives' = activeArchives \ {ArchiveFor(p)}
    /\ UNCHANGED <<agedArchives, archiveExists, archiveContents, currentPDF,
                   bundle, digest, archivePath, pdfFiles, revisions,
                   readerStarted, readerPDF, readerRevisions, readerFetched>>

CommitAccepted(p) ==
    /\ p \in Publishers
    /\ phase[p] = "accepted"
    /\ phase' = [phase EXCEPT ![p] = "committed"]
    /\ activeArchives' = activeArchives \ {ArchiveFor(p)}
    /\ currentPDF' = PDFFor(p)
    /\ bundle' = BundleFor(p)
    /\ digest' = DigestFor(p)
    /\ archivePath' = ArchiveFor(p)
    /\ pdfFiles' = pdfFiles \cup {PDFFor(p)}
    /\ revisions' = revisions \cup {PDFFor(p)}
    /\ UNCHANGED <<agedArchives, archiveExists, archiveContents, readerStarted,
                   readerPDF, readerRevisions, readerFetched>>

ReadState ==
    /\ ~readerStarted
    /\ readerStarted' = TRUE
    /\ readerPDF' = currentPDF
    /\ readerRevisions' = revisions
    /\ UNCHANGED <<phase, activeArchives, agedArchives, archiveExists,
                   archiveContents, currentPDF, bundle, digest, archivePath,
                   pdfFiles, revisions, readerFetched>>

FetchReaderPDF ==
    /\ readerStarted
    /\ ~readerFetched
    /\ readerPDF \in pdfFiles
    /\ readerPDF \in readerRevisions
    /\ readerFetched' = TRUE
    /\ UNCHANGED <<phase, activeArchives, agedArchives, archiveExists,
                   archiveContents, currentPDF, bundle, digest, archivePath,
                   pdfFiles, revisions, readerStarted, readerPDF, readerRevisions>>

Next == \/ \E p \in Publishers : BeginPublish(p)
        \/ \E p \in Publishers : LongPendingPush(p)
        \/ \E p \in Publishers : SeamAccept(p)
        \/ \E p \in Publishers : PublishReject(p)
        \/ \E p \in Publishers : CommitAccepted(p)
        \/ ReadState
        \/ FetchReaderPDF
        \/ UNCHANGED vars

Spec == Init /\ [][Next]_vars

TypeOK ==
    /\ phase \in [Publishers -> Phases]
    /\ activeArchives \subseteq CandidateArchives
    /\ agedArchives \subseteq CandidateArchives
    /\ archiveExists \subseteq Archives
    /\ archiveContents \in [Archives -> (Bundles \cup {"none"})]
    /\ currentPDF \in PDFs
    /\ bundle \in Bundles
    /\ digest \in Digests
    /\ archivePath \in Archives
    /\ pdfFiles \subseteq PDFs
    /\ revisions \subseteq PDFs
    /\ readerStarted \in BOOLEAN
    /\ readerPDF \in PDFs
    /\ readerRevisions \subseteq PDFs
    /\ readerFetched \in BOOLEAN

VisibleArtifactsCoherent ==
    /\ currentPDF \in pdfFiles
    /\ currentPDF \in revisions
    /\ archivePath \in archiveExists
    /\ archiveContents[archivePath] = bundle
    /\ digest = IF bundle = "old-bundle" THEN "old-digest"
                ELSE IF bundle = "slow-bundle" THEN "slow-digest"
                ELSE "fast-digest"

ReaderSnapshotReadable ==
    /\ ~readerStarted
       \/ /\ readerPDF \in readerRevisions
          /\ readerPDF \in pdfFiles

ReaderFetchMatchesSnapshot ==
    /\ ~readerFetched
       \/ /\ readerPDF \in readerRevisions
          /\ readerPDF \in pdfFiles

=============================================================================
