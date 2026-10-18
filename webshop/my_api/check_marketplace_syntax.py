"""Syntax check for the marketplace files. Run once, then delete:

	bench execute webshop.my_api.check_marketplace_syntax.execute
	or: python -c "import ast,sys;[ast.parse(open(f,encoding='utf-8').read(),f) for f in sys.argv[1:]]" <files>
"""

import ast
import os

FILES = [
	"webshop/webshop/doctype/marketplace_shop/marketplace_shop.py",
	"webshop/webshop/doctype/marketplace_application/marketplace_application.py",
	"webshop/webshop/doctype/marketplace_payout/marketplace_payout.py",
	"webshop/webshop/doctype/marketplace_payout_order/marketplace_payout_order.py",
	"webshop/webshop/my_api/marketplace.py",
	"webshop/webshop/my_api/marketplace_admin.py",
	"webshop/webshop/my_api/vendor_api.py",
	"webshop/webshop/my_api/admin_api.py",
	"webshop/webshop/my_api/api.py",
	"webshop/webshop/setup/marketplace.py",
	"webshop/webshop/hooks.py",
]


def execute():
	basedir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
	failed = []
	for rel in FILES:
		path = os.path.join(basedir, rel)
		if not os.path.exists(path):
			failed.append((rel, "missing"))
			continue
		with open(path, encoding="utf-8") as fh:
			try:
				ast.parse(fh.read(), path)
			except SyntaxError as exc:
				failed.append((rel, "line %s: %s" % (exc.lineno, exc.msg)))
	if failed:
		return {"ok": False, "errors": failed}
	return {"ok": True, "checked": len(FILES)}
