// Copyright (c) 2026, ws and contributors
// For license information, please see license.txt

frappe.listview_settings["Page Builder"] = {
	onload(listview) {
		if (!frappe.user.has_role("System Manager")) return;

		// --- Bulk: migrate the selected pages' images to S3 and reclaim disk ---
		listview.page.add_action_item(__("Migrate Images to S3"), () => {
			const names = listview.get_checked_items(true); // true -> docnames only
			if (!names || !names.length) {
				frappe.msgprint(__("Select at least one page first."));
				return;
			}

			frappe.confirm(
				__(
					"Migrate {0} selected page(s) to S3 — including all version-history " +
						"snapshots — and delete the local copies to reclaim disk? " +
						"Restore still works (snapshots are rewritten to S3). " +
						"Images already on S3 are skipped. This cannot be undone.",
					[names.length]
				),
				() => {
					frappe.call({
						method: "prism.api.page_builder.bulk_migrate_images_to_s3",
						args: { names },
						freeze: true,
						freeze_message: __("Migrating images for {0} page(s)…", [names.length]),
						callback: (r) => {
							const m = r.message || {};
							if (!m.success) {
								frappe.msgprint({
									title: __("Migration failed"),
									indicator: "red",
									message: m.error || __("Could not migrate images."),
								});
								return;
							}

							const d = m.data || {};
							const t = d.totals || {};
							let msg = __("{0} page(s) migrated, {1} failed.", [
								d.pages_ok || 0,
								d.pages_failed || 0,
							]);
							msg +=
								"<br>" +
								__("{0} image(s) to S3, {1} snapshot(s) rewritten, {2} local file(s) deleted.", [
									t.migrated || 0,
									t.snapshots || 0,
									t.deleted || 0,
								]);
							if (t.failed)
								msg += "<br>" + __("{0} image(s) failed (kept local; see Error Log).", [t.failed]);

							frappe.msgprint({
								title: __("Bulk migration complete"),
								indicator: d.pages_failed ? "orange" : "green",
								message: msg,
							});
							listview.clear_checked_items();
							listview.refresh();
						},
					});
				}
			);
		});
	},
};
