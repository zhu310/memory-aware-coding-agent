import os


# Test doubles and intentional failure paths must not pollute production audit logs.
os.environ["LLM_USAGE_LOG_ENABLED"] = "0"
