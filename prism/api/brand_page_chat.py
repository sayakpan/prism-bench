"""
Persistence for the Brand Page Builder AI chat.

One "Brand Page AI Chat" thread holds an ordered list of "Brand Page AI Message"
rows (standalone doctype, linked by `chat`). Messages are never hard-deleted or
renumbered: delete sets `is_deleted=1` (the row stays, `seq` is preserved) and all
reads filter `is_deleted=0`. `seq` is allocated per-chat from the chat's `next_seq`
counter under a row lock, so concurrent appends never collide.

Gated to internal users (authenticated, non-Buyer) and scoped by brand — mirrors
the auth/response conventions in prism.api.page_builder.
"""

import base64
import io
import json
import mimetypes

import frappe
from frappe.utils import now

from prism.auth.authenticator import auth_required
import prism.api.util as util
import prism.lib.cloud as cloud

ALLOWED_ROLES = {"User", "Assistant", "Clarify", "System"}
ALLOWED_MODES = {"", "Page", "Section"}
JSON_FIELDS = ("steps", "warnings", "qa", "answers", "attachments")
MAX_TITLE_LEN = 140
MAX_REFERENCE_IMAGE_MB = 5

CHAT_DOCTYPE = "Brand Page AI Chat"
MESSAGE_DOCTYPE = "Brand Page AI Message"


# --- write ----------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
@auth_required
def create_chat(brand_id: str, title: str = None):
	""" Creates an empty chat thread for a brand. """
	try:
		_ensure_internal()

		if not brand_id:
			return {"success": False, "error": "Brand ID is required!"}
		if not frappe.db.exists("Brand", brand_id):
			return {"success": False, "error": "Brand does not exist!"}

		doc = _new_chat(brand_id, title)
		frappe.db.commit()

		return {"success": True, "data": {"chat_id": doc.name, "title": doc.title}}

	except Exception as ex:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "brand_page_chat.create_chat()")
		return {"success": False, "error": str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def append_message(chat_id: str = None, brand_id: str = None, role: str = None,
                   text: str = None, steps=None, warnings=None, qa=None,
                   answers=None, vibe_id: str = None, mode: str = None,
                   attachments=None):
	"""
	Appends a message to a chat, lazily creating the chat if only brand_id is
	given. Returns the new message's id and its allocated seq.
	"""
	try:
		_ensure_internal()

		role = (role or "").strip()
		if role not in ALLOWED_ROLES:
			return {"success": False, "error": f"Invalid role: {role or '(empty)'}"}

		mode = (mode or "").strip()
		if mode not in ALLOWED_MODES:
			return {"success": False, "error": f"Invalid mode: {mode}"}

		# Resolve the target chat (or lazy-create from brand_id).
		if chat_id:
			if not frappe.db.exists(CHAT_DOCTYPE, chat_id):
				return {"success": False, "error": "Chat does not exist!"}
			chat_name = chat_id
		elif brand_id:
			if not frappe.db.exists("Brand", brand_id):
				return {"success": False, "error": "Brand does not exist!"}
			chat_name = _new_chat(brand_id, text).name
		else:
			return {"success": False, "error": "chat_id or brand_id is required!"}

		seq = _alloc_seq(chat_name)

		msg = frappe.new_doc(MESSAGE_DOCTYPE)
		msg.chat = chat_name
		msg.seq = seq
		msg.role = role
		msg.mode = mode or None
		msg.vibe_id = vibe_id
		msg.text = text
		msg.steps = _json_text(steps)
		msg.warnings = _json_text(warnings)
		msg.qa = _json_text(qa)
		msg.answers = _json_text(answers)
		msg.attachments = _json_text(_offload_attachments(chat_name, attachments))
		msg.is_deleted = 0
		msg.insert(ignore_permissions=True)

		_refresh_chat_stats(chat_name)
		frappe.db.commit()

		return {
			"success": True,
			"data": {"chat_id": chat_name, "message_id": msg.name, "seq": seq},
		}

	except Exception as ex:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "brand_page_chat.append_message()")
		return {"success": False, "error": str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def delete_message(message_id: str):
	""" Soft-deletes a single message (row stays, seq preserved). """
	try:
		_ensure_internal()

		if not message_id or not frappe.db.exists(MESSAGE_DOCTYPE, message_id):
			return {"success": False, "error": "Message does not exist!"}

		chat_name, already_deleted = frappe.db.get_value(
			MESSAGE_DOCTYPE, message_id, ["chat", "is_deleted"]
		)

		if not already_deleted:
			frappe.db.set_value(
				MESSAGE_DOCTYPE, message_id,
				{"is_deleted": 1, "deleted_at": now(), "deleted_by": frappe.session.user},
				update_modified=False,
			)
			_refresh_chat_stats(chat_name)
			frappe.db.commit()

		return {"success": True, "data": {"ok": True}}

	except Exception as ex:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "brand_page_chat.delete_message()")
		return {"success": False, "error": str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def clear_chat(chat_id: str):
	""" Soft-deletes every message in a chat. The chat row itself stays. """
	try:
		_ensure_internal()

		if not chat_id or not frappe.db.exists(CHAT_DOCTYPE, chat_id):
			return {"success": False, "error": "Chat does not exist!"}

		frappe.db.set_value(
			MESSAGE_DOCTYPE,
			{"chat": chat_id, "is_deleted": 0},
			{"is_deleted": 1, "deleted_at": now(), "deleted_by": frappe.session.user},
			update_modified=False,
		)
		frappe.db.set_value(CHAT_DOCTYPE, chat_id, "message_count", 0,
		                    update_modified=False)
		frappe.db.commit()

		return {"success": True, "data": {"ok": True}}

	except Exception as ex:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "brand_page_chat.clear_chat()")
		return {"success": False, "error": str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def upload_reference_image(chat_id: str):
	"""
	Streams a multipart-uploaded reference image straight to S3 and returns its
	servable URL, so the frontend can convert composer base64 refs into URLs before
	appending. The file rides the request as multipart form-data under the `file`
	key. Nothing is written to Frappe assets — the object only ever lives in the
	bucket, keyed by the chat.
	"""
	try:
		_ensure_internal()

		if not chat_id or not frappe.db.exists(CHAT_DOCTYPE, chat_id):
			return {"success": False, "error": "Chat does not exist!"}

		files = getattr(frappe.request, "files", None)
		upload = files.get("file") if files else None
		if not upload:
			return {"success": False, "error": "No file provided!"}

		content = upload.stream.read()
		if not content:
			return {"success": False, "error": "Uploaded file is empty!"}
		if len(content) > MAX_REFERENCE_IMAGE_MB * 1024 * 1024:
			return {"success": False,
			        "error": f"File exceeds {MAX_REFERENCE_IMAGE_MB} MB limit!"}

		filename = upload.filename or f"ref_{frappe.generate_hash()[:8]}.png"
		key = cloud.build_brand_page_chat_attachment_key(chat_id, cloud.file_ext(filename))
		cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))

		return {"success": True, "data": {"file_url": cloud.asset_url(key)}}

	except Exception as ex:
		frappe.log_error(frappe.get_traceback(), "brand_page_chat.upload_reference_image()")
		return {"success": False, "error": str(ex)}


# --- read -----------------------------------------------------------------

@frappe.whitelist(allow_guest=True)
@auth_required
def list_chats(brand_id: str, limit: int = 20, offset: int = 0):
	""" Lists a brand's chats, newest first. """
	try:
		_ensure_internal()

		if not brand_id:
			return {"success": False, "error": "Brand ID is required!"}

		filters = {"brand": brand_id}
		chats = frappe.get_all(
			CHAT_DOCTYPE,
			filters=filters,
			fields=["name as id", "title", "last_active", "message_count"],
			order_by="last_active desc, creation desc",
			limit_page_length=int(limit or 20),
			limit_start=int(offset or 0),
		)
		total = frappe.db.count(CHAT_DOCTYPE, filters)

		return {"success": True, "data": {"chats": chats, "total": total}}

	except Exception as ex:
		frappe.log_error(frappe.get_traceback(), "brand_page_chat.list_chats()")
		return {"success": False, "error": str(ex)}


@frappe.whitelist(allow_guest=True)
@auth_required
def get_chat(chat_id: str, limit: int = 200, offset: int = 0):
	""" Returns a chat header plus its non-deleted messages, ordered by seq. """
	try:
		_ensure_internal()

		if not chat_id or not frappe.db.exists(CHAT_DOCTYPE, chat_id):
			return {"success": False, "error": "Chat does not exist!"}

		chat = frappe.get_doc(CHAT_DOCTYPE, chat_id)

		rows = frappe.get_all(
			MESSAGE_DOCTYPE,
			filters={"chat": chat_id, "is_deleted": 0},
			fields=["name as message_id", "seq", "role", "mode", "vibe_id", "text",
			        "error", "steps", "warnings", "qa", "answers", "attachments",
			        "creation"],
			order_by="seq asc",
			limit_page_length=int(limit or 200),
			limit_start=int(offset or 0),
		)
		for r in rows:
			for f in JSON_FIELDS:
				r[f] = frappe.parse_json(r[f]) if r.get(f) else None

		return {
			"success": True,
			"data": {
				"chat": {
					"id": chat.name,
					"brand": chat.brand,
					"title": chat.title,
					"last_active": chat.last_active,
					"message_count": chat.message_count,
				},
				"messages": rows,
			},
		}

	except Exception as ex:
		frappe.log_error(frappe.get_traceback(), "brand_page_chat.get_chat()")
		return {"success": False, "error": str(ex)}


# --- helpers --------------------------------------------------------------

def _ensure_internal():
	""" Gate: authenticated (via auth_required) internal staff, not a Buyer. """
	if util.user_has_roles(["Buyer"]):
		frappe.throw("Not authorized to use Brand Page chat!", frappe.PermissionError)


def _new_chat(brand_id: str, title_source: str = None):
	""" Inserts a fresh chat for a brand and returns the doc. """
	doc = frappe.new_doc(CHAT_DOCTYPE)
	doc.brand = brand_id
	doc.title = _clean_title(title_source)
	doc.message_count = 0
	doc.next_seq = 1
	doc.last_active = now()
	doc.insert(ignore_permissions=True)
	return doc


def _clean_title(value):
	value = (value or "").strip()
	if not value:
		return "New chat"
	return value[:MAX_TITLE_LEN]


def _alloc_seq(chat_name: str):
	"""
	Atomically read-and-increment the chat's next_seq under a row lock, so
	concurrent appends to the same chat get distinct, monotonic seq values.
	"""
	row = frappe.db.sql(
		"SELECT `next_seq` FROM `tabBrand Page AI Chat` WHERE `name` = %s FOR UPDATE",
		chat_name,
	)
	seq = int(row[0][0]) if row and row[0][0] is not None else 1
	frappe.db.set_value(CHAT_DOCTYPE, chat_name, "next_seq", seq + 1,
	                    update_modified=False)
	return seq


def _refresh_chat_stats(chat_name: str):
	""" Recompute non-deleted message_count and bump last_active. """
	count = frappe.db.count(MESSAGE_DOCTYPE, {"chat": chat_name, "is_deleted": 0})
	frappe.db.set_value(
		CHAT_DOCTYPE, chat_name,
		{"message_count": count, "last_active": now()},
		update_modified=False,
	)


def _offload_attachments(chat_name: str, attachments):
	"""
	Guarantee every attachment reference lives on S3 before it is stored.

	The frontend is expected to upload via upload_reference_image and send back S3
	URLs, but this is the backstop for the two ways media could otherwise leak onto
	the app server: a base64 `data:` URI posted inline, or a staged local /files
	path. Both are pushed to the bucket and rewritten to the servable URL; anything
	already on S3 (or any foreign URL) is returned untouched, so this is idempotent
	and cheap on the normal path.
	"""
	if attachments is None or attachments == "":
		return None

	parsed = frappe.parse_json(attachments) if isinstance(attachments, str) else attachments
	if parsed is None:
		return None

	build_key = lambda ext: cloud.build_brand_page_chat_attachment_key(chat_name, ext)
	return _walk_offload(parsed, build_key)


def _walk_offload(node, build_key):
	""" Recursively rewrite local/base64 image refs anywhere in the payload. """
	if isinstance(node, str):
		return _offload_ref(node, build_key)
	if isinstance(node, list):
		return [_walk_offload(v, build_key) for v in node]
	if isinstance(node, dict):
		return {k: _walk_offload(v, build_key) for k, v in node.items()}
	return node


def _offload_ref(value: str, build_key):
	if value.startswith("data:"):
		return _offload_base64(value, build_key)
	if value.startswith("/files/") or value.startswith("/private/files/"):
		return _offload_local_file(value, build_key)
	return value


def _offload_base64(value: str, build_key):
	""" Decode an inline data: URI and put the bytes straight into the bucket. """
	header, _, b64 = value.partition(",")
	if not b64:
		return value

	mime = ""
	if ":" in header and ";" in header:
		mime = header.split(":", 1)[1].split(";", 1)[0]
	ext = mimetypes.guess_extension(mime) or ".png"

	content = base64.b64decode(b64)
	_check_size(len(content))

	key = build_key(ext)
	cloud.upload_file(io.BytesIO(content), key, mime or cloud.DEFAULT_FILE_CONTENT_TYPE)
	return cloud.asset_url(key)


def _offload_local_file(value: str, build_key):
	""" Move a staged local Frappe File into the bucket and drop the local copy. """
	file_name = frappe.db.get_value("File", {"file_url": value}, "name")
	if not file_name:
		return value  # nothing on disk to move; leave the value as-is

	file_doc = frappe.get_doc("File", file_name)
	content = file_doc.get_content()
	_check_size(len(content))

	filename = file_doc.file_name or value
	key = build_key(cloud.file_ext(filename))
	cloud.upload_file(io.BytesIO(content), key, cloud.content_type_for(filename))
	frappe.delete_doc("File", file_doc.name, ignore_permissions=True, force=True)

	return cloud.asset_url(key)


def _check_size(size_bytes: int):
	if size_bytes > MAX_REFERENCE_IMAGE_MB * 1024 * 1024:
		frappe.throw(f"Attachment exceeds the maximum allowed limit of {MAX_REFERENCE_IMAGE_MB} MB.")


def _json_text(value):
	""" Normalize an incoming JSON param (object or JSON string) to storable text. """
	if value is None or value == "":
		return None
	if isinstance(value, str):
		try:
			json.loads(value)
			return value
		except Exception:
			return json.dumps(value)
	return json.dumps(value, default=str)
