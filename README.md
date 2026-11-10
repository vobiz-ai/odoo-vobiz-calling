# Vobiz Calling for Odoo

Make and take [Vobiz](https://www.vobiz.ai) phone calls inside Odoo: a phone in
the top bar, click-to-call on every phone number, and every call logged on the
contact and under **Vobiz > Calls**.

This repository holds the Odoo module `vobiz_calling`, published on the Odoo
Apps Store. There is one branch per Odoo version; this is `18.0`.

## Requirements

- Odoo 18 (Community or Enterprise), on Odoo.sh or your own server. Odoo Online
  cannot install modules that contain Python code.
- Odoo reachable from the internet over HTTPS: Vobiz sends each call's events to it.
- A Vobiz account with at least one active voice number.

## Install

1. Copy the `vobiz_calling` folder into your Odoo add-ons path (on Odoo.sh, add
   it to your repository).
2. Restart Odoo, turn on developer mode, then click **Apps > Update Apps List**.
3. Search for **Vobiz Calling** and click **Install**.

Setting it up and using it: [vobiz_calling/README.md](vobiz_calling/README.md).

## Support

support@vobiz.ai

## License

LGPL-3. The bundled JsSIP library is MIT licensed
(`vobiz_calling/static/lib/jssip/LICENSE`).
