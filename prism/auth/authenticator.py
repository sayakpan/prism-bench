import secrets
import requests
import jwt
from jwt import PyJWKClient, ExpiredSignatureError, InvalidTokenError
from functools import wraps
from datetime import datetime, timezone, timedelta

import frappe

#import prism.api.da as da
import prism.api.util as util

AUTH0_DOMAIN = 'pratibhasyntex.au.auth0.com'   #frappe.conf.get('auth0_domain')
AUTH0_CLIENT_ID = 'RyXsQEEwZf834h1nNVprBXM4tkRrUfNA'
AUTH0_CLIENT_SECRET = 'n1GtlHWioEqocn7Kzn9_RYCv19XmCwfupRLqM6U2W9loUYyqdk2XYGK5nigNsZyR'
#JWT_SECRET = '12f60113e7eb69034026b8ab207fe40955e7c16bdf973e2cc2a74843053605af'
JWT_ALGORITHM = 'HS256'
ACCESS_TOKEN_EXPIRE_MINUTES = 60
REFRESH_TOKEN_EXPIRE_DAYS = 30


@frappe.whitelist(allow_guest=True)
def auth0_login(id_token):
    ''' Auth0 login '''

    try:
        idinfo = _decode_auth0_user(id_token)

        if idinfo.get('success'):
            auth0_user = idinfo['user']
            
            # get user email
            email = auth0_user.get('email')
            if not email:
                raise ValueError('Could not retrieve auth email')
            
            # check if email is verified
            #if not auth0_user.get('email_verified'):
            #    raise ValueError('Auth email is not verified')
            
            # check if user exists
            user_exists = frappe.db.exists('User', email)
            
            if user_exists:
                user_info, tokens = generate_tokens(email)
                return {
                    'success': True,
                    'user_info': user_info,
                    'auth_tokens': {**tokens}
                }
            else:   # user not yet registered but saved draft Buyer/Seller
                return {
                    'success': True,
                    'user_info': {
                        'id': email,
                        'email': email,
                        'name': auth0_user.get('name')
                    }
                }
        else:
            return {
                'success': False,
                'error': idinfo.get('error')
            }
    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }

@frappe.whitelist(allow_guest=True)
def refresh_access_token(refresh_token):
    ''' Validates refresh token and issues a new access token '''

    ret_obj = { 'success': False }

    try:
        payload = jwt.decode(
            refresh_token,
            AUTH0_CLIENT_SECRET,
            algorithms=[JWT_ALGORITHM]
        )

        # ensure token type is refresh
        if payload.get('type') != 'refresh':
            raise ValueError('Invalid token type')

        user_id = payload.get('user_id')
        if not user_id:
            raise ValueError('User ID not found in token')

        # check jti against DB / cache for revocation
        # jti = payload.get('jti')
        # if is_token_revoked(jti):
        #     raise ValueError('Refresh token revoked')

        # issue new access token
        user = frappe.get_doc('User', user_id)
        user_info = util.get_user_info(user)
        new_access_token = _generate_access_token(user_info)

        ret_obj = {
            'success': True,
            'access_token': new_access_token
        }

    except ExpiredSignatureError:
        ret_obj['error'] = 'Refresh token has expired'
    except InvalidTokenError:
        ret_obj['error'] = 'Invalid refresh token'
    except Exception as e:
        ret_obj['error'] = f'Could not refresh access token: {str(e)}'

    return ret_obj


def set_user_from_jwt_header():
    '''
    before_request hook: if X-Auth-Token is present and valid, set the
    Frappe session user from the JWT. Silent on missing/invalid token —
    Frappe's own whitelist / allow_guest check then decides whether to
    reject the request.
    '''
    token = frappe.get_request_header('X-Auth-Token')
    if not token:
        return

    try:
        payload = jwt.decode(
            token,
            AUTH0_CLIENT_SECRET,
            algorithms=[JWT_ALGORITHM]
        )

        user_email = payload.get('user', {}).get('email')
        if not user_email:
            return

        if not frappe.db.exists('User', user_email):
            return

        # frappe.set_user() wipes frappe.local.form_dict as a side effect
        # (see frappe/__init__.py set_user). Save and restore so that query
        # params survive the auth swap — Frappe's own auth validators do the
        # same dance in frappe/auth.py.
        saved_form_dict = frappe.local.form_dict
        frappe.set_user(user_email)
        frappe.local.form_dict = saved_form_dict
        frappe.local.jwt_payload = payload

    except (ExpiredSignatureError, InvalidTokenError):
        return
    except Exception:
        return


def auth_required(func):
    @wraps(func)
    def decorated_function(*args, **kwargs):
        # get Authorization header
        token = frappe.get_request_header('X-Auth-Token')
        
        if not token:
            frappe.throw(
                'Missing Authorization header',
                frappe.AuthenticationError
            )
        
        # check for Bearer token format
        #parts = auth_header.split()
        #if len(parts) != 2 or parts[0].lower() != 'bearer':
        #    frappe.throw(
        #        "Invalid Authorization header format. Expected 'Bearer <token>'",
        #        frappe.AuthenticationError
        #    )
        #token = parts[1]
        
        try:
            # decode and validate the token
            payload = jwt.decode(
                token,
                AUTH0_CLIENT_SECRET,
                algorithms=[JWT_ALGORITHM]
            )
            
            # extract user email from payload
            user_email = payload.get('user', {}).get('email')
            
            if not user_email:
                frappe.throw(
                    'Token missing user identifier',
                    frappe.AuthenticationError
                )
            
            # verify user exists in Frappe
            if not frappe.db.exists('User', user_email):
                frappe.throw(
                    'User not found',
                    frappe.AuthenticationError
                )
            
            # set the user session
            frappe.set_user(user_email)
            
            # Optionally store payload for later use
            frappe.local.jwt_payload = payload
            
        except jwt.ExpiredSignatureError:
            frappe.throw('Token has expired', frappe.AuthenticationError)
        except jwt.InvalidTokenError as e:
            frappe.throw(f'Invalid token: {str(e)}', frappe.AuthenticationError)
        except Exception as e:
            frappe.throw(f'Error in processing auth token: {str(e)}', frappe.AuthenticationError)
        
        return func(*args, **kwargs)

    return decorated_function


@frappe.whitelist(allow_guest=True, methods=['POST'])
@auth_required
def create_socket_session():
    '''
    Mints a Frappe login session for the JWT-authenticated user and returns its
    sid, so a decoupled (cross-origin) frontend can authenticate the realtime
    socket via Frappe's standard cookie path — no Frappe-core change.

    The SPA stores the returned sid as a first-party cookie on its own domain; an
    nginx /socket.io proxy (Host + Origin rewritten to the bench) forwards it to
    the bench, which validates it like any desk session and joins the socket to
    the user's `user:<email>` room. Cookies ride the WebSocket handshake, so this
    needs no polling.

    Additive: does not touch the existing JWT login flow (auth0_login). Must be
    POST so the new session row is committed (Frappe rolls back writes on GET).
    '''
    try:
        user = util.get_current_user_id()
        frappe.local.login_manager.login_as(user)
        frappe.db.commit()
        return {
            'success': True,
            'sid': frappe.session.sid,
            'user': user,
        }
    except Exception as e:
        frappe.db.rollback()
        frappe.log_error(frappe.get_traceback(), 'authenticator.create_socket_session()')
        return {'success': False, 'error': str(e)}


def get_auth0_user(id_token):
    ''' Extract user info from Auth0 id-token. '''

    ret_obj = { 'success': False }

    try:
        auth0_res = _decode_auth0_user(id_token)

        if not auth0_res['success']:
            return auth0_res
        
        auth0_user = auth0_res['user']
        user_email = auth0_user['email']

        # if user exists in db, add roles
        if frappe.db.exists('User', user_email):
            user_doc = frappe.get_doc('User', user_email)
            auth0_user['roles'] = [d.role for d in user_doc.roles]
        else:
            auth0_user['roles'] = []

        ret_obj = {
            'success': True,
            'user': auth0_user
        }

    except Exception as e:
        ret_obj['error'] = f'Error verifying token: {str(e)}'

    return ret_obj

def generate_tokens(user_id: str):
    '''
    Generate access token and refresh token for a user
    '''

    user = frappe.get_doc('User', user_id)

    # check if user is enabled
    if user.enabled == 0:
        raise ValueError('User is disabled.')

    user_info = util.get_user_info(user)

    # buyer/seller not suspended/rejected
    if not user_info['enabled']:
        raise ValueError('User is disabled.')

    # api keys
    api_keys = _generate_api_keys(user)
    user_info['api_access'] = api_keys

    access_token = _generate_access_token(user_info)
    refresh_token = _generate_refresh_token(user_info)
    
    # Store refresh token in database
    #_store_refresh_token(user_info.get('id'), refresh_token)
    
    return user_info, {
        'access_token': access_token,
        'refresh_token': refresh_token,
        'token_type': 'bearer',
        'expires_in': ACCESS_TOKEN_EXPIRE_MINUTES * 60
    }

def on_user_after_insert(doc, method=None):
    ''' Frappe doc-event: mirror a newly inserted User into Auth0 '''

    if doc.name in ('Administrator', 'Guest'):
        return

    result = _add_auth0_user(doc.full_name or doc.name, doc.email or doc.name)

    if not result.get('success'):
        frappe.log_error(
            f'Auth0 user creation failed for {doc.name}: {result.get("error")}',
            'authenticator.on_user_after_insert'
        )


# -- helper functions ---
def _add_auth0_user(user_full_name: str, user_email: str):
    ''' adds a user in Auth0 '''

    ret_obj = { 'success': False }

    try:
        mgmt_token = _get_auth0_mgmt_token()

        url = f'https://{AUTH0_DOMAIN}/api/v2/users'
        headers = {
            'Authorization': f'Bearer {mgmt_token}',
            'Content-Type': 'application/json'
        }
        payload = {
            'email': user_email,
            'name': user_full_name,
            'connection': 'Username-Password-Authentication',
            'password': secrets.token_urlsafe(24) + 'Aa1!',
            #email_verified': False,
            'verify_email': True,
            #'user_metadata': {
            #    'role': doc.role, # e.g., 'Manufacturer' or 'Brand'
            #    'frappe_id': doc.name
            # }
        }

        response = requests.post(url, json=payload, headers=headers, timeout=10)

        if response.status_code == 201:
            ret_obj = {
                'success': True,
                'user': response.json()
            }
        else:
            err = response.json() if response.content else {}
            ret_obj['error'] = err.get('message') or err.get('error') or f'Auth0 returned status {response.status_code}'
            frappe.log_error(frappe.get_traceback(), 'authenticator.add_auth0_user()/inner')

    except Exception as e:
        frappe.log_error(frappe.get_traceback(), 'authenticator.add_auth0_user()')
        ret_obj['error'] = f'Error creating Auth0 user: {str(e)}'

    return ret_obj

def _get_auth0_mgmt_token():
    ''' Fetch a Management API access token via client credentials grant '''

    url = f'https://{AUTH0_DOMAIN}/oauth/token'
    payload = {
        'client_id': AUTH0_CLIENT_ID,
        'client_secret': AUTH0_CLIENT_SECRET,
        'audience': f'https://{AUTH0_DOMAIN}/api/v2/',
        'grant_type': 'client_credentials'
    }
    response = requests.post(url, json=payload, timeout=10)
    response.raise_for_status()
    return response.json()['access_token']

def _decode_auth0_user(id_token):
    ''' Extract user info from Auth0 id-token. '''

    ret_obj = { 'success': False }

    try:
        # fetch JWKS (JSON Web Key Set) from Auth0
        jwks_url = f'https://{AUTH0_DOMAIN}/.well-known/jwks.json'
        jwks_client = PyJWKClient(jwks_url)
        
        # get the signing key
        signing_key = jwks_client.get_signing_key_from_jwt(id_token)
        
        # decode and verify the token
        decoded = jwt.decode(
            id_token,
            signing_key.key,
            algorithms=['RS256'],
            audience=AUTH0_CLIENT_ID,
            issuer=f'https://{AUTH0_DOMAIN}/'
        )
        
        user_email = decoded.get('email')
        user_info = {
            'name': decoded.get('name'),
            'email': user_email,
            'email_verified': decoded.get('email_verified')
            #"given_name": decoded.get("given_name"),
            #"family_name": decoded.get("family_name"),
            #"picture": decoded.get("picture"),
            #"sub": decoded.get("sub")
        }

        ret_obj = {
            'success': True,
            'user': user_info
        }
        
    except jwt.ExpiredSignatureError:
        ret_obj['error'] = 'Token has expired'
    except jwt.InvalidAudienceError:
        ret_obj['error'] = 'Invalid token audience'
    except jwt.InvalidIssuerError:
        ret_obj['error'] = 'Invalid token issuer'
    except Exception as e:
        ret_obj['error'] = f'Error verifying token: {str(e)}'

    return ret_obj

def _generate_access_token(user_info):
    '''
    Generate JWT access token
    '''
    payload = {
        'user': user_info,
        'exp': datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES),
        'iat': datetime.now(timezone.utc),
        'type': 'access'
    }
    
    token = jwt.encode(payload, AUTH0_CLIENT_SECRET, algorithm=JWT_ALGORITHM)
    return token

def _generate_refresh_token(user_info):
    '''
    Generate JWT refresh token
    '''
    payload = {
        'user_id': user_info['id'],
        'exp': datetime.now(timezone.utc) + timedelta(days=REFRESH_TOKEN_EXPIRE_DAYS),
        'iat': datetime.now(timezone.utc),
        'type': 'refresh',
        'jti': secrets.token_urlsafe(32)  # Unique identifier
    }
    
    token = jwt.encode(payload, AUTH0_CLIENT_SECRET, algorithm=JWT_ALGORITHM)
    return token

def _generate_api_keys(user_doc):
    """ Generate user API keys. """

    # check if API Key already exists
    if not user_doc.api_key:
        # generate new keys if they don't exist
        user_doc.api_key = frappe.generate_hash(length=15)
        
    # always generate a new API Secret for security
    api_secret = frappe.generate_hash(length=15)
    user_doc.api_secret = api_secret
    
    # save the user document
    user_doc.save(ignore_permissions=True)

    return {
        'key': user_doc.api_key,
        'secret': api_secret
    }


def revoke_refresh_token(user, jti):
    '''
    Revoke a specific refresh token
    '''
    frappe.db.set_value('JwtRefreshToken', {'user': user, 'jti': jti}, 'is_revoked', 1)
    frappe.db.commit()

def revoke_all_user_tokens(user):
    ''' Revoke all refresh tokens for a user (logout from all devices) '''
    #return da.revoke_all_user_tokens(user)
    pass

def cleanup_expired_tokens():
    ''' Remove expired tokens from database (run as scheduled job) '''
    #return da.cleanup_expired_tokens()
    pass


# ------------------------------------------
@frappe.whitelist(allow_guest=True)
def get_test_token(email):
    try:
        if frappe.db.exists('User', email):
            user_info, tokens = generate_tokens(email)
            return {
                'success': True,
                'tokens': tokens,
                'user_info': user_info
            }
        else:
            return {
                'success': False,
                'error': 'User does not exist!'
            }

    except Exception as e:
        return {
            'success': False,
            'error': str(e)
        }
