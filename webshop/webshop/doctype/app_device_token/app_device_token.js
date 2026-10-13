frappe.ui.form.on('App Device Token', {
    refresh(frm) {
        // Read-only indicator in form view
        if (frm.doc.is_guest) {
            frm.set_df_property('user', 'read_only', 1);
        }
    }
});