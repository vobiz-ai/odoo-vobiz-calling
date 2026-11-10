import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";
import { PhoneField } from "@web/views/fields/phone/phone_field";

/**
 * Click-to-call: a phone number's link in Odoo dials it with the Vobiz phone
 * when the agent is signed in to it. Signed out, the link does what it always
 * did (the browser's tel: handler).
 */
patch(PhoneField.prototype, {
    setup() {
        super.setup(...arguments);
        this.vobiz = useService("vobiz_softphone");
    },

    onVobizCall(ev) {
        const number = this.props.record.data[this.props.name];
        if (!number || !this.vobiz.state.signedIn) {
            return;
        }
        ev.preventDefault();
        ev.stopPropagation();
        const name = this.props.record.data.display_name || this.props.record.data.name || "";
        this.vobiz.call(number, typeof name === "string" ? name : "");
    },
});
