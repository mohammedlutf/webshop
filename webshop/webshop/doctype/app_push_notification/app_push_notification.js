frappe.ui.form.on('App Push Notification', {
	refresh(frm) {
		// Add "Send/Resend Notification" button if form is not dirty
		if (!frm.is_dirty()) {
			const label = frm.doc.status === 'Sent' ? __('Resend Notification') : __('Send Notification Now');
			frm.add_custom_button(label, function() {
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