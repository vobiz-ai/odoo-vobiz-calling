{
    "name": "Vobiz Calling",
    "version": "18.0.1.0.0",
    "summary": "Make and take Vobiz phone calls in Odoo, with every call logged",
    "description": """
A softphone in Odoo's top bar, on your Vobiz account: call any number or
click a phone number on a contact, take incoming calls with Accept or
Decline, and find every call logged on the contact and under Vobiz > Calls.

Requires a Vobiz account (vobiz.ai), and an Odoo reachable from the internet
over HTTPS. To make and take calls, this module sends Vobiz the phone numbers
dialled and calling in, and receives each call's times and result. Nothing is
sent until an administrator ticks "Allow calls through Vobiz" in Settings.
""",
    "category": "Productivity/VoIP",
    "author": "Vobiz",
    "maintainer": "Vobiz",
    "website": "https://www.vobiz.ai",
    "support": "support@vobiz.ai",
    "license": "LGPL-3",
    "images": ["static/description/banner.png"],
    "depends": ["base_setup", "mail", "bus"],
    "external_dependencies": {"python": ["cryptography", "requests"]},
    "data": [
        "security/ir.model.access.csv",
        "security/vobiz_security.xml",
        "data/ir_cron.xml",
        "views/vobiz_endpoint_views.xml",
        "views/res_config_settings_views.xml",
        "views/vobiz_call_views.xml",
    ],
    "assets": {
        "web.assets_backend": [
            "vobiz_calling/static/src/**/*",
        ],
    },
    "application": True,
    "installable": True,
}
