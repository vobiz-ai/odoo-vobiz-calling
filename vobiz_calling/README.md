# Vobiz Calling for Odoo

Make and take Vobiz phone calls inside Odoo 18. A phone icon in the top bar
holds the softphone; phone numbers anywhere in Odoo become click-to-call; and
every call is logged, on the contact and under **Vobiz → Calls**.

Odoo itself is the calling backend: the module answers Vobiz's webhooks on the
customer's own Odoo, so no other server is involved. Agents sign in one of two
ways: with an **access token** (JWT), a 15-minute token Odoo has Vobiz mint, so
the browser never receives an Auth Token; or as a **SIP endpoint**, with the
endpoint's password, which stays in the browser and never reaches Odoo.

## Requirements

- Odoo 18 (Community or Enterprise), self-hosted or on Odoo.sh. Odoo Online
  cannot install modules that contain Python.
- A Vobiz account with at least one active voice number.
- Odoo reachable by Vobiz over **HTTPS**. Calls are controlled through webhooks
  to this Odoo, and browsers only allow the microphone on secure pages.

## Setup (an administrator, once)

1. Install **Vobiz Calling** from Apps.
2. Open **Settings → Vobiz** (or **Vobiz → Settings**).
3. **Public URL for Vobiz:** the https:// address of this Odoo. Leave it empty
   to use the web base URL.
4. **Auth ID** and **Auth Token:** from the Vobiz Console, under API credentials.
   The token is stored encrypted and is never shown again; leave the box empty
   to keep the saved one.
5. Click **Save and check connection**. Odoo checks the credentials, creates one
   Vobiz application named `Odoo-Calling (<your host>)` whose answer URL is
   `<public URL>/vobiz/answer` (or writes those again on an existing one), calls
   itself at the Public URL the way Vobiz will, and picks a **Default caller ID**.
   The line under the button says what that check found, and **Last check** says
   when; nothing there is live, so check again after changing the address, the
   proxy or the tunnel. A new Public URL moves and renames the application.
6. To take incoming calls, click **Ring Odoo for calls to this number**. This
   points the default number at Odoo's application, moving it off any other
   application it was on; the confirmation says so before anything changes.
7. Optional, for sign-in with SIP endpoints: click **Manage SIP endpoints** (or
   open **Vobiz → SIP endpoints**), then **Load from Vobiz**, then **Use with
   Odoo** on each endpoint agents may sign in with. That links the endpoint to
   Odoo's application in Vobiz, since Vobiz sends a call placed from an endpoint
   to the application linked to it. Set **Agent** to keep an endpoint for one
   user. **Stop using** signs out whoever uses it and blocks it in Odoo; Vobiz
   keeps it linked until you change that in the Vobiz Console.

## Using it (every agent)

Click the **phone icon** in the top bar. There are two ways in:

- **Access token (JWT).** **Sign in** uses the company's Vobiz account. **Use my
  own Auth ID and Auth Token** signs in with another account instead; those are
  kept, encrypted, only until the agent signs out or the sign-in expires (12
  hours). Either way the browser gets short-lived tokens, renewed by itself.
- **SIP endpoint.** The username and password of an endpoint an administrator
  allowed (step 7). Only the username goes to Odoo; the password stays in the
  page, so after a reload the panel asks for it again. One person at a time can
  use an endpoint.

Then:

- **Calling from** chooses which of the account's numbers calls go out from.
- Type a number or use the keypad and press **Call**, or click any phone number
  in Odoo.
- During a call: **Mute**, **Hold**, **Keypad** (tones for phone menus) and
  **Hang up**.
- An incoming call opens the panel, rings, and shows the caller (and the
  contact's name when the number belongs to one) with **Accept** and
  **Decline**. The caller is not answered, or billed, until someone accepts.
  Declined by everyone, or not answered within the ring time, the caller hears
  a busy tone.

One sign-in per agent: signing in again (another tab, another browser) ends the
previous one. Calls keep going while you move between Odoo screens; reloading
the page ends a call in progress. An access-token sign-in picks up again by
itself after a reload; a SIP endpoint asks for its password again.

## How calls work

- **Outbound.** The browser calls Vobiz; Vobiz asks `/vobiz/answer`, which puts
  the browser in a private conference room and has Vobiz dial the customer
  into it from the chosen number. (A `<Dial><Number>` straight from a browser
  leg was measured losing the customer's audio on three calls in five.)
  Hanging up before the customer answers cancels the call to them. A customer
  who declines is reported by their mobile network only some 20–30 seconds
  later, and the panel shows **Ringing** until then. If Vobiz refuses the call
  before it starts, the panel checks whether this Odoo answers at its Public URL
  and says so when it does not.
- **Inbound.** `/vobiz/answer` rings every agent signed in to the account that
  owns the number, with `<Dial><User>`. The caller stays unanswered while it
  rings; `/vobiz/dial-status` turns away a caller nobody took with a busy tone.
- **Logging.** Odoo receives every event of a call, so each call is logged once:
  a `vobiz.call` record, and a note on the matching contact's chatter.

## Security

- Every webhook must carry a valid Vobiz signature (`X-Vobiz-Signature-V3`,
  HMAC-SHA256 over the URL and a nonce) made with the Auth Token of the account
  it belongs to. Unsigned or forged webhooks get `403` and change nothing.
- Auth Tokens are encrypted with a key derived from the database's secret, and
  readable only by administrators (company) or not at all (agents' own).
- The browser gets a short-lived access token, never the Auth Token. Webhook
  query strings are never trusted: an outbound room is named by a random,
  unguessable value and everything else is looked up on the server.
- **Test copies.** When Odoo neutralizes a copy of the database (an Odoo.sh
  staging or development database, or a duplicate or restore made with
  **Neutralize**), the copy drops the Vobiz Auth Tokens, the consent, the link
  to the live application, the Public URL and every sign-in
  (`data/neutralize.sql`). It cannot take the live numbers' calls. To try calls
  in a copy, enter credentials there on purpose, ideally from a separate Vobiz
  test account: **Ring Odoo for calls to this number** in the copy moves that
  number to the copy.

## Limitations

- Calls ring and are made only in an open, signed-in Odoo tab. The Odoo mobile
  app does not ring.
- Reloading the page ends a call in progress.
- No hold, transfer, mute or recording yet.
- Contacts are matched on phone and mobile. Unknown numbers are logged under
  **Vobiz → Calls** without creating a contact.
