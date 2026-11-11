-- A neutralized copy (an Odoo.sh staging or development database, or a
-- duplicate or restore made with "Neutralize") must not act for the live
-- database. With the live Auth Token it could point the live numbers' calls at
-- itself, place calls on the live account, and accept Vobiz's webhooks.
-- Drop the Auth Token, the consent and the link to the live application; the
-- Auth ID, the numbers and the call history stay. Entering credentials again in
-- the copy then gives it an application at its own address, and the live
-- application keeps ringing the live database.
UPDATE res_company
   SET vobiz_auth_token_enc = NULL,
       vobiz_data_consent = false,
       vobiz_app_id = NULL,
       vobiz_app_url = NULL,
       vobiz_checked_on = NULL,
       vobiz_checked_url = NULL,
       vobiz_reach_ok = false;

-- the live database's public address: the copy uses its own web address
DELETE FROM ir_config_parameter
 WHERE key = 'vobiz_calling.public_url';

-- end every sign-in, with the agents' own Auth Tokens and access tokens
DELETE FROM vobiz_room;
DELETE FROM vobiz_session;
