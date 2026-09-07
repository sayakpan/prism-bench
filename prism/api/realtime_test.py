import frappe


@frappe.whitelist(allow_guest=True)
def auth_test(user: str = '', message: str = ''):
    ''' Fires a Socket.IO realtime event named `auth_test` so you can verify
        that an external Socket.IO client is connected *as a real user* (not Guest).

        Hit this endpoint:  /api/method/prism.api.realtime_test.auth_test

        Targeting:
          - By default the event is published to `frappe.session.user` (whoever's
            session/cookie made THIS http request).
          - Pass `?user=<email>` to publish to a specific user's room instead.

        How to read the result:
          - If your socket client is authenticated as the same user, it WILL receive
            the `auth_test` event -> auth/cookie/CORS path is working.
          - If the client only keeps showing ping/pong and never gets `auth_test`,
            that socket is connected as Guest (its sid cookie isn't reaching the
            socketio server).

        The JSON response below tells you who the event was actually sent to, so you
        can confirm it matches the user your socket client is logged in as.
    '''
    target_user = (user or '').strip() or frappe.session.user

    payload = {
        'ok': True,
        'message': (message or '').strip() or 'auth_test event received',
        'sent_to': target_user,
        'request_user': frappe.session.user,
    }

    # user-scoped: only the target user's room receives this. A Guest socket
    # never joined that room, so receiving it proves the connection is authed.
    frappe.publish_realtime('auth_test', payload, user=target_user)

    return {
        'published_event': 'auth_test',
        'sent_to': target_user,
        'request_user': frappe.session.user,
        'note': 'Listen for the "auth_test" event on your socket client. '
                'If it does not arrive, the socket is connected as Guest.',
    }
