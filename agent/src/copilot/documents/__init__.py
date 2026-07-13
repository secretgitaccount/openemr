"""Document ingestion package (Week 2).

Home for the clinical-document pipeline: attach a PDF, extract structured
fields with a VLM, validate against Pydantic schemas, and write the result
back to OpenEMR. Concrete logic lands in later PRPs (PRP-02/04/05/06); this
module only establishes the importable package boundary.
"""
