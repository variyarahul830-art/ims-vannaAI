"""
generate_token.py
=================
Generates a valid JWT token for the InternHub AI Assistant.
Use this token in your Authorization header: 'Bearer <token>'
Or paste it into the 'bearer-token' attribute of the <vanna-chat> component.
"""

import jwt
import os
import datetime
from dotenv import load_dotenv

load_dotenv()

SECRET = os.getenv("JWT_SECRET", "a9a5bacd60ccc5c1ee2f070c50004f724e8d25f0ad568e0740a9f9ea9beb881c")
ALGO   = os.getenv("JWT_ALGORITHM", "HS256")

# Correct claims for InternHub AI Assistant
payload = {
    "sub": "admin_user_001",
    "name": "Super Admin",
    "role": "super_admin", # STRICT requirement for access
    "exp": datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=7)
}

token = jwt.encode(payload, SECRET, algorithm=ALGO)

print("\n--- GENERATED TOKEN FOR SUPER ADMIN ---")
print(token)
print("----------------------------------------\n")
print(f"Role: {payload['role']}")
print(f"Expires: {payload['exp']}")
