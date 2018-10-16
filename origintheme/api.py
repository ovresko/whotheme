from __future__ import unicode_literals

import frappe
import frappe.defaults

import pprint
pp = pprint.PrettyPrinter(indent=4)

from erpnext.shopping_cart.doctype.shopping_cart_settings.shopping_cart_settings import get_shopping_cart_settings
from frappe.utils.nestedset import get_root_of
from frappe import throw, _
from frappe.utils import flt

def on_session_creation(login_manager):
	print "On session create called"
	info = frappe.db.get_value("User", frappe.local.session_obj.user,
			["home_page_link"], as_dict=1)

	frappe.local.response["home_page"] = info.home_page_link or "/desk"

@frappe.whitelist()
def get_sidebar_template():
	cur_user = frappe.get_doc('User',frappe.session.user)
	return cur_user.sidebar_template

@frappe.whitelist()
def place_order():
	quotation = _get_cart_quotation()
	quotation.company = frappe.db.get_value("Shopping Cart Settings", None, "company")
	if not quotation.get("customer_address"):
		throw(_("{0} is required").format(_(quotation.meta.get_label("customer_address"))))

	quotation.flags.ignore_permissions = True
	quotation.submit()

	if quotation.lead:
		# company used to create customer accounts
		frappe.defaults.set_user_default("company", quotation.company)

@frappe.whitelist()
def get_item_price(item_code):
	customer = user_to_customer(frappe.session.user)
	if customer is None:
		return None
	customer = frappe.get_doc('Customer', customer)

	today = frappe.utils.nowdate()

	shopping_cart_settings = frappe.get_doc('Shopping Cart Settings', 'Shopping Cart Settings')

	price = frappe.get_all("Item Price", fields=["price_list_rate", "currency"],
			filters={"price_list": shopping_cart_settings.price_list, "item_code": item_code})
	if not price:
		return None
	price = price[0]

	pricing_rules = frappe.db.sql(""" SELECT * FROM
		`tabPricing Rule`
		WHERE apply_on='Item Code'
		AND item_code = %(item_code)s
		AND applicable_for = 'Customer'
		AND customer = %(customer)s
		AND (valid_from IS NULL OR valid_from <= %(today)s)
		AND (valid_upto IS NULL OR valid_upto >= %(today)s)
		ORDER BY priority DESC;
		""", {
		"item_code": item_code,
		"customer": customer.name,
		"today": today}, as_dict=1)

	if pricing_rules and len(pricing_rules) > 0:
		pricing_rule = pricing_rules[0]
		if pricing_rule['price_or_discount'] == "Discount Percentage":
			price['price_list_rate'] = flt(price['price_list_rate'] * (1.0 - (pricing_rule['discount_percentage'] / 100.0)))
		if pricing_rule['price_or_discount'] == "Price":
			price['price_list_rate'] = pricing_rule['price']

	item = frappe.get_doc('Item', item_code)

	price['stock_uom'] = item.stock_uom

	if price['price_list_rate']:
		return price

	return None






def user_to_customer(user):
	"""
	Accepts name of user, returns name of coresponding customer_name
	"""
	user = frappe.get_doc('User', user)
	is_customer = False
	for role in user.roles:
		if role.role == 'Customer':
			is_customer = True
	if not is_customer:
		return None
	contacts = frappe.get_all('Contact',
		filters={
			"user": user.name
		},
		fields=['name'])

	for contact in contacts:
		contact = frappe.get_doc('Contact', contact['name'])
		for link in contact.links:
			if link.link_doctype == 'Customer':
				return link.link_name
	return None

def _get_cart_quotation(party=None):
	'''Return the open Quotation of type "Shopping Cart" or make a new one'''
	if not party:
		party = get_party()

	quotation = frappe.get_all("Quotation", fields=["name"], filters=
		{party.doctype.lower(): party.name, "order_type": "Shopping Cart", "docstatus": 0},
		order_by="modified desc", limit_page_length=1)

	if quotation:
		qdoc = frappe.get_doc("Quotation", quotation[0].name)
	else:
		qdoc = frappe.get_doc({
			"doctype": "Quotation",
			"naming_series": get_shopping_cart_settings().quotation_series or "QTN-CART-",
			"quotation_to": party.doctype,
			"company": frappe.db.get_value("Shopping Cart Settings", None, "company"),
			"order_type": "Shopping Cart",
			"status": "Draft",
			"docstatus": 0,
			"__islocal": 1,
			(party.doctype.lower()): party.name
		})

		qdoc.contact_person = frappe.db.get_value("Contact", {"email_id": frappe.session.user})
		qdoc.contact_email = frappe.session.user

		qdoc.flags.ignore_permissions = True
		qdoc.run_method("set_missing_values")
		# apply_cart_settings(party, qdoc)

	return qdoc

def get_party(user=None):
	from frappe.utils import get_fullname
	if not user:
		user = frappe.session.user

	contact_name = frappe.db.get_value("Contact", {"email_id": user})
	party = None

	if contact_name:
		contact = frappe.get_doc('Contact', contact_name)
		if contact.links:
			party_doctype = contact.links[0].link_doctype
			party = contact.links[0].link_name

	cart_settings = frappe.get_doc("Shopping Cart Settings")

	debtors_account = ''

	if cart_settings.enable_checkout:
		debtors_account = get_debtors_account(cart_settings)

	if party:
		return frappe.get_doc(party_doctype, party)

	else:
		if not cart_settings.enabled:
			frappe.local.flags.redirect_location = "/contact"
			raise frappe.Redirect
		customer = frappe.new_doc("Customer")
		fullname = get_fullname(user)
		customer.update({
			"customer_name": fullname,
			"customer_type": "Individual",
			"customer_group": get_shopping_cart_settings().default_customer_group,
			"territory": get_root_of("Territory")
		})

		if debtors_account:
			customer.update({
				"accounts": [{
					"company": cart_settings.company,
					"account": debtors_account
				}]
			})

		customer.flags.ignore_mandatory = True
		customer.insert(ignore_permissions=True)

		contact = frappe.new_doc("Contact")
		contact.update({
			"first_name": fullname,
			"email_id": user
		})
		contact.append('links', dict(link_doctype='Customer', link_name=customer.name))
		contact.flags.ignore_mandatory = True
		contact.insert(ignore_permissions=True)

		return customer

def get_debtors_account(cart_settings):
	from erpnext.accounts.utils import get_account_name
	payment_gateway_account_currency = \
		frappe.get_doc("Payment Gateway Account", cart_settings.payment_gateway_account).currency

	account_name = _("Debtors ({0})".format(payment_gateway_account_currency))

	debtors_account_name = get_account_name("Receivable", "Asset", is_group=0,\
		account_currency=payment_gateway_account_currency, company=cart_settings.company)

	if not debtors_account_name:
		debtors_account = frappe.get_doc({
			"doctype": "Account",
			"account_type": "Receivable",
			"root_type": "Asset",
			"is_group": 0,
			"parent_account": get_account_name(root_type="Asset", is_group=1, company=cart_settings.company),
			"account_name": account_name,
			"currency": payment_gateway_account_currency
		}).insert(ignore_permissions=True)

		return debtors_account.name

	else:
		return debtors_account_name
