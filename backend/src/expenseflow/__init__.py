"""ExpenseFlow shared library, imported by every Lambda handler.

Modules (import them explicitly; this package deliberately imports nothing so a handler only pays the
cold-start cost of what it uses):

* ``settings``      - typed, lazy access to the environment variables CDK injects
* ``aws``           - cached boto3 clients/resources (never created at import time)
* ``ids``           - ULIDs, timestamps and month helpers
* ``keys``          - every DynamoDB key format of the single-table design
* ``models``        - enums, API request models (pydantic) and the API response mapping
* ``auth``          - the authenticated caller, built from Cognito JWT claims
* ``errors``        - domain errors that map to HTTP status codes
* ``serialization`` - Decimal-safe JSON and pagination cursors
* ``events``        - EventBridge event names, detail builders and publishing
* ``repository``    - expense data access with conditional writes
* ``receipts``      - receipt extraction with Claude on Amazon Bedrock
"""
