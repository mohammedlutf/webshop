frappe.ui.form.on('App Push Notification', {
    refresh(frm) {
        // Add "Send Notification" button in form view if not sent
        if (frm.doc.status !== 'Sent' && !frm.is_dirty()) {
            frm.add_custom_button(__('Send Notification Now'), function() {
                frappe.confirm(
                    `Are you sure you want to send this push notification to <b>${frm.doc.target_audience}</b>?`,
                    function() {
                        frappe.call({
                            doc: frm.doc,
                            method: 'send_push_notification',
                            freeze: true,
                            freeze_message: __('Sending Push Notifications...'),
                            callback: function(r) {
                                if (r.message && r.message.status === 'completed') {
                                    frappe.msgprint(__('Successfully sent to ') + r.message.delivered_count + __(' devices!'));
                                    frm.reload_doc();
                                }
                            }
                        });
                    }
                );
            }).addClass('btn-primary');
        }
    }
});