"""
JWT token issue/verification and Google auth validation.
Supports two identity types: Google-authenticated users and ephemeral guests.
"""

import os
import json
import jwt
import warnings
from datetime import datetime, timedelta
from typing import Dict, Optional
from fastapi import HTTPException, status
from enum import Enum


class IdentityType(str, Enum):
    GOOGLE = "google"
    GUEST = "guest"


def get_jwt_secret() -> str:
    """Retrieve JWT secret from environment; fail fast if missing."""
    secret = os.getenv("JWT_SECRET")
    if not secret:
        # Allow local development without extra env setup.
        # Hosted environments should always provide JWT_SECRET explicitly.
        if os.getenv("RAILWAY_ENVIRONMENT") or os.getenv("RAILWAY_PROJECT_ID"):
            raise RuntimeError(
                "JWT_SECRET environment variable not set. "
                "Set it before starting the server."
            )
        warnings.warn(
            "JWT_SECRET is not set. Using insecure development fallback secret.",
            RuntimeWarning,
            stacklevel=2,
        )
        return "dev-insecure-jwt-secret-change-me"
    return secret


def create_jwt(user_id: str, identity_type: IdentityType, email: Optional[str] = None) -> str:
    """
    Create a JWT token for either a Google user or guest.
    
    Args:
        user_id: unique identifier (email for Google, random UUID for guest)
        identity_type: IdentityType.GOOGLE or IdentityType.GUEST
        email: user email (for Google users only)
    
    Returns:
        JWT token string
    """
    secret = get_jwt_secret()
    
    # Expiry: 7 days for authenticated users, 30 days for guests
    expiry_days = 7 if identity_type == IdentityType.GOOGLE else 30
    exp = datetime.utcnow() + timedelta(days=expiry_days)
    
    payload = {
        "user_id": user_id,
        "identity_type": identity_type,
        "exp": exp,
        "iat": datetime.utcnow(),
    }
    
    if email:
        payload["email"] = email
    
    token = jwt.encode(payload, secret, algorithm="HS256")
    return token


def verify_jwt(token: str) -> Dict:
    """
    Verify and decode a JWT token.
    
    Args:
        token: JWT token string (without "Bearer " prefix)
    
    Returns:
        Decoded token payload (contains user_id, identity_type, email if present)
    
    Raises:
        HTTPException with 401 if token is invalid or expired
    """
    secret = get_jwt_secret()
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        return payload
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token expired",
        )
    except jwt.InvalidTokenError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
        )


def extract_token_from_header(auth_header: Optional[str]) -> str:
    """
    Extract Bearer token from Authorization header.
    
    Args:
        auth_header: Authorization header value (e.g., "Bearer {token}")
    
    Returns:
        Token string
    
    Raises:
        HTTPException with 403 if header missing or malformed
    """
    if not auth_header:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Authorization header required",
        )
    
    parts = auth_header.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid authorization header format. Use: Bearer {token}",
        )
    
    return parts[1]


def get_current_user(auth_header: Optional[str]) -> Dict:
    """
    Resolve current user from Authorization header.
    Used as a FastAPI dependency for protected endpoints.
    
    Args:
        auth_header: Authorization header value
    
    Returns:
        User identity dict (user_id, identity_type, email if present)
    
    Raises:
        HTTPException with 401/403 if auth fails
    """
    token = extract_token_from_header(auth_header)
    user = verify_jwt(token)
    return user


def verify_google_token(id_token: str, expected_client_id: Optional[str] = None) -> Dict:
    """
    Verify a Google ID token received from frontend.
    
    This is a minimal verification using only standard JWT decode.
    For production, use google.auth.transport.requests or google-auth-httplib2.
    
    Args:
        id_token: ID token from Google Sign-In
        expected_client_id: Expected Google Client ID to verify (optional)
    
    Returns:
        Token payload (sub, email, name, etc.)
    
    Raises:
        HTTPException with 401 if token is invalid
    
    Note:
        In production, you should verify the signature with Google's public keys.
        For Stage 1 MVP, this does basic structural validation.
    """
    try:
        # Decode without verification first to get header info (alg, kid).
        # In production, fetch Google's public key using kid and verify signature.
        unverified = jwt.decode(id_token, options={"verify_signature": False})
        
        # Basic validation: must have sub (user ID) and email.
        if "sub" not in unverified or "email" not in unverified:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid Google token: missing sub or email",
            )
        
        # If client ID provided, verify aud matches.
        if expected_client_id and unverified.get("aud") != expected_client_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Google token audience mismatch",
            )
        
        return unverified
    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Google token format",
        )
