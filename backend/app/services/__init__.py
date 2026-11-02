"""Business logic, grouped by the stage of the flow each module belongs to.

    tender/       receive a tender, collect its attachments, read its pages,
                  ingest its items and requirements, run the pipeline
    requirement/  a clause turned into a typed requirement
    corpus/       the manufacturer corpus: read it, seed it, restore it, audit it
    match/        a requirement against the corpus: retrieve, extract, verify,
                  score, record
    output/       what a run produces: its state and its report
    cli/          commands that drive the whole flow

Three modules stay at the top because every layer imports them and no stage owns
them: `llm` (the client), `embeddings` (the vector side), `textnorm` (shared
text normalisation).

The move renamed a module only where the package already carried its name:

    tenderupload   -> tender/upload        corpus        -> corpus/run
    tendercollect  -> tender/collect       corpusseed    -> corpus/seed
    tenderingest   -> tender/ingest        corpusrestore -> corpus/restore
    docparser      -> tender/parse         matching      -> match/match
    pageclassify   -> tender/classify      retrieval     -> match/retrieve
    atomizer       -> requirement/atomize  verifier      -> match/verify
    retyping       -> requirement/retype   scoring       -> match/score
    normalizer     -> requirement/normalize categories   -> match/category
    resolution     -> requirement/resolve  tenderrecovery-> cli/recover
    extractor      -> requirement/extract  ingestion     -> corpus/ingest
                                           report        -> output/report
                                           runs          -> output/runs

A decision recorded before the move names a module by its old path; this table
is the map from one to the other.
"""
