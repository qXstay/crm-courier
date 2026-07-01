from decimal import Decimal
import os
from pathlib import Path
import re
import unittest

os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")
os.environ.setdefault("SECRET_KEY", "test-secret")

from app.templating import money, phone


class TemplatingFiltersTest(unittest.TestCase):
    def test_money_uses_russian_decimal_separator(self):
        cases = [
            (Decimal("800.00"), "800 ₽"),
            (Decimal("36.50"), "36,50 ₽"),
            (Decimal("2961.50"), "2 961,50 ₽"),
        ]

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(money(value), expected)

    def test_phone_normalizes_display_without_space_after_plus(self):
        cases = [
            ("+ 7 995 995-95-95", "+7 995 995-95-95"),
            ("89959959595", "+7 995 995-95-95"),
            ("+7 995 995-95-95", "+7 995 995-95-95"),
            (None, "Не указан"),
        ]

        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(phone(value), expected)


class TemplateUxTest(unittest.TestCase):
    def test_login_and_user_forms_accept_plain_login_and_password_toggle(self):
        login_template = Path("app/templates/auth/login.html").read_text(encoding="utf-8")
        user_form_template = Path("app/templates/users/form.html").read_text(encoding="utf-8")
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn('name="email" type="text"', login_template)
        self.assertIn(">Логин</label>", login_template)
        self.assertIn('label for="email">Логин *', user_form_template)
        self.assertIn('name="email" type="text"', user_form_template)
        self.assertIn('placeholder="manager1"', user_form_template)
        self.assertIn("data-password-toggle=\"password\"", user_form_template)
        self.assertIn("Показать пароль", user_form_template)
        self.assertIn(".password-input-group", styles)
        self.assertIn(".password-toggle", styles)

    def test_clients_template_has_smooth_search_scaffold(self):
        template = Path("app/templates/clients/list.html").read_text(encoding="utf-8")

        self.assertIn("data-client-filter-form", template)
        self.assertIn("data-client-filter-input", template)
        self.assertIn("data-client-filter-item", template)
        self.assertIn("Найдено клиентов:", template)
        self.assertIn("applyClientFilter", template)

    def test_payments_template_has_server_filter_and_hidden_initial_results(self):
        template = Path("app/templates/payments/list.html").read_text(encoding="utf-8")
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn("data-payment-filter-form", template)
        self.assertIn("work-page-wrap payments-page-wrap", template)
        self.assertIn("data-payment-filter-input", template)
        self.assertIn("data-payment-filter-item", template)
        self.assertIn("Неоплаченные заявки", template)
        self.assertIn("Выберите дату, статус или поиск.", template)
        self.assertIn('name="detail"', template)
        self.assertIn('name="view"', template)
        self.assertIn("payment-stat-row", template)
        self.assertIn("Сводка по текущему списку.", template)
        self.assertIn("Открыт список неоплаченных заявок.", template)
        self.assertIn('filters.view == "unpaid"', template)
        self.assertIn("Оплаченные заявки скрыты в этом списке", template)
        self.assertIn("payment-paid-callout", template)
        self.assertIn("focus_order_id", template)
        self.assertIn("payment-focus-row", template)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr));", styles)
        self.assertIn(".payment-paid-callout", styles)
        self.assertIn(".payment-focus-row td", styles)
        self.assertNotIn("applyPaymentFilter", template)

    def test_route_desktop_stats_are_compact_and_courier_action_is_clear(self):
        list_template = Path("app/templates/couriers/list.html").read_text(encoding="utf-8")
        route_template = Path("app/templates/couriers/route.html").read_text(encoding="utf-8")
        screen_template = Path("app/templates/couriers/_route_screen.html").read_text(encoding="utf-8")
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn("work-page-wrap route-page-wrap", list_template)
        self.assertIn("work-page-wrap route-page-wrap", route_template)
        self.assertIn('class="action-link route-open-link"', screen_template)
        self.assertIn(">Выбрать</a>", screen_template)
        self.assertIn(".route-page-wrap,\n.payments-page-wrap {", styles)
        self.assertIn(".route-stat-list {\n  grid-template-columns: repeat(3, minmax(0, 1fr));", styles)
        self.assertIn(".route-open-link {\n    width: 100%;\n    min-height: 44px;", styles)

    def test_orders_template_has_quick_status_and_payment_forms(self):
        template = Path("app/templates/orders/list.html").read_text(encoding="utf-8")

        self.assertIn('/orders/{{ order.id }}/status', template)
        self.assertIn('/orders/{{ order.id }}/quick-pay', template)
        self.assertIn('data-order-return-link', template)
        self.assertIn('data-order-return-input', template)
        self.assertIn('name="cash_amount"', template)
        self.assertIn('name="card_amount"', template)
        self.assertIn('aria-label="Сохранить статус"', template)
        self.assertIn("data-pickup-copy", template)
        self.assertIn("data-pickup-copy-text", template)
        self.assertIn("номер груза", template)
        self.assertIn("номер карго", template)
        self.assertIn("navigator.clipboard.writeText", template)
        self.assertIn("fallbackCopyText", template)
        self.assertIn("Скопировано", template)

    def test_orders_list_uses_crm_modal_for_quick_cancel_status(self):
        template = Path("app/templates/orders/list.html").read_text(encoding="utf-8")

        self.assertNotIn("window.confirm", template)
        self.assertIn("data-quick-status-cancel-modal", template)
        self.assertIn("Отменить заявку?", template)
        self.assertIn("Заявка попадёт в архив. Восстановить сможет админ.", template)
        self.assertIn("Не отменять", template)
        self.assertIn("Отменить заявку", template)

    def test_orders_desktop_row_shows_current_status_near_number(self):
        template = Path("app/templates/orders/list.html").read_text(encoding="utf-8")

        self.assertIn("order-number-cell", template)
        self.assertIn("order-row-status {{ order.status|status_class }}", template)
        self.assertIn("{{ order.status|status_label }}", template)

    def test_orders_template_has_date_status_and_courier_filters(self):
        template = Path("app/templates/orders/list.html").read_text(encoding="utf-8")

        self.assertIn("Дата доставки с", template)
        self.assertIn("Дата доставки по", template)
        self.assertIn('name="date_from"', template)
        self.assertIn('name="date_to"', template)
        self.assertIn('id="filter-status" name="status"', template)
        self.assertIn('id="filter-courier" name="courier_id"', template)
        self.assertIn("data-order-filter-param=\"filter_status\"", template)
        self.assertIn("data-order-filter-param=\"filter_courier_id\"", template)

    def test_orders_filter_submit_builds_clean_query_string(self):
        template = Path("app/templates/orders/list.html").read_text(encoding="utf-8")

        self.assertIn("const orderFilterUrl = () =>", template)
        self.assertIn("const params = new URLSearchParams();", template)
        self.assertIn('params.set("status", current.filter_status);', template)
        self.assertIn('params.set("courier_id", current.filter_courier_id);', template)
        self.assertIn('return query ? `/orders?${query}` : "/orders";', template)

    def test_courier_list_limits_long_address_and_note_previews(self):
        template = Path("app/templates/couriers/dashboard.html").read_text(encoding="utf-8")
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn("courier-text-preview", template)
        self.assertIn("courier-note-preview", template)
        self.assertIn("courier-card-preview", template)
        self.assertIn(".courier-orders-table {\n  min-width: 0;\n  table-layout: fixed;", styles)
        self.assertIn("-webkit-line-clamp: 3;", styles)
        self.assertIn("-webkit-line-clamp: 4;", styles)

    def test_orders_page_uses_wide_desktop_wrap(self):
        base_template = Path("app/templates/base.html").read_text(encoding="utf-8")
        orders_template = Path("app/templates/orders/list.html").read_text(encoding="utf-8")
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn('{% block wrap_class %}{% endblock %}', base_template)
        self.assertIn('{% block wrap_class %} work-page-wrap orders-page-wrap{% endblock %}', orders_template)
        self.assertIn(".work-page-wrap {", styles)
        self.assertIn("max-width: none;", styles)

    def test_mobile_order_card_shows_finance_before_contact_details_once(self):
        template = Path("app/templates/orders/list.html").read_text(encoding="utf-8")
        card_template = template.split('<article class="order-card"', maxsplit=1)[1]

        self.assertLess(card_template.index("order-card-finance"), card_template.index("Телефон"))
        self.assertEqual(card_template.count('<div class="meta-label">Стоимость</div>'), 1)
        self.assertEqual(card_template.count('<div class="meta-label">Оплата</div>'), 1)

    def test_mobile_orders_and_bottom_nav_contain_long_content(self):
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn(".order-card .meta-value,", styles)
        self.assertIn(".order-card .cargo-pill,", styles)
        self.assertIn("overflow-wrap: anywhere;", styles)
        self.assertIn("white-space: normal;", styles)
        self.assertIn(".bottom-nav ul {", styles)
        self.assertIn("width: 100%;\n  min-width: 0;\n  max-width: 100%;", styles)
        self.assertIn("min-width: 76px;\n  flex: 1 0 76px;", styles)
        self.assertIn(".mobile-account-logout button {\n  min-height: 44px;", styles)
        self.assertIn(
            ".quick-status-form-card select,\n"
            "  .quick-status-form-card .quick-status-submit {\n"
            "    min-height: 44px;",
            styles,
        )

    def test_order_form_client_validation_lists_required_fields(self):
        template = Path("app/templates/orders/form.html").read_text(encoding="utf-8")

        self.assertIn("Укажите ФИО клиента", template)
        self.assertIn("Укажите телефон", template)
        self.assertIn("Укажите адрес доставки", template)

    def test_order_form_places_status_before_courier_field(self):
        template = Path("app/templates/orders/form.html").read_text(encoding="utf-8")

        self.assertLess(template.index('label for="status"'), template.index('label for="courier-id"'))

    def test_order_form_hides_create_costs_and_old_note_labels(self):
        template = Path("app/templates/orders/form.html").read_text(encoding="utf-8")

        self.assertIn("{% if is_edit %}", template)
        self.assertIn("Стоимость и расходы", template)
        self.assertIn("Заполните данные клиента, доставки и груза.", template)
        self.assertIn("Заполните данные клиента, доставки, груза и расходов.", template)
        self.assertIn("Примечание к заявке", template)
        self.assertNotIn("Проверим клиента по телефону и ФИО", template)
        self.assertNotIn(">Примечание от клиента</label>", template)
        self.assertNotIn(">Примечание для сотрудников</label>", template)

    def test_order_detail_has_cancel_button_and_conditional_note_summary(self):
        template = Path("app/templates/orders/detail.html").read_text(encoding="utf-8")

        self.assertIn("Отменить заявку", template)
        self.assertNotIn(">В архив</button>", template)
        self.assertNotIn("window.confirm", template)
        self.assertIn("data-cancel-order-modal", template)
        self.assertIn("Заявка попадёт в архив. Восстановить сможет админ.", template)
        self.assertIn("Не отменять", template)
        self.assertIn("order-notes-summary", template)
        self.assertIn("Заметка клиента", template)
        self.assertIn("Примечание к заявке", template)
        self.assertIn("order.created_by.full_name", template)
        self.assertIn("order.archived_by.full_name", template)

    def test_archive_template_links_duplicate_cargo_restore_error(self):
        template = Path("app/templates/archive/list.html").read_text(encoding="utf-8")

        self.assertIn("Не удалось восстановить. Заявка с таким номером груза уже есть:", template)
        self.assertIn("duplicate_cargo_order.order_code", template)
        self.assertIn('href="/orders/{{ duplicate_cargo_order.id }}"', template)

    def test_accounting_template_explains_period_submit_flow(self):
        template = Path("app/templates/accounting/list.html").read_text(encoding="utf-8")

        self.assertIn("Выберите день или месяц, дату и нажмите «Показать».", template)
        self.assertIn("syncAccountingPeriodState", template)

    def test_brand_assets_are_connected_in_templates(self):
        base_template = Path("app/templates/base.html").read_text(encoding="utf-8")
        login_template = Path("app/templates/auth/login.html").read_text(encoding="utf-8")

        self.assertIn('/static/img/favicon.svg', base_template)
        self.assertIn('Courier CRM', base_template)
        self.assertIn('Courier CRM', login_template)

    def test_mobile_logout_is_available_in_bottom_navigation(self):
        base_template = Path("app/templates/base.html").read_text(encoding="utf-8")
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn('class="mobile-account-logout"', base_template)
        self.assertIn('action="/logout"', base_template)
        self.assertIn(">Выйти</button>", base_template)
        self.assertIn(".mobile-account-logout", styles)

    def test_mobile_bottom_navigation_stays_one_scrollable_row(self):
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn("flex-wrap: nowrap;", styles)
        self.assertIn("overflow-x: auto;", styles)
        self.assertIn("scrollbar-width: none;", styles)
        self.assertIn("overscroll-behavior-x: contain;", styles)

    def test_payments_actions_are_compact_on_desktop(self):
        template = Path("app/templates/payments/list.html").read_text(encoding="utf-8")
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn("payment-cell-flow", template)
        self.assertIn("payment-pay-block", template)
        self.assertIn("payment-amount-due", template)
        self.assertIn(".payment-btn-sm {", styles)
        self.assertIn("payment-cell-flow {", styles)
        self.assertIn("payment-pay-block {", styles)

    def test_courier_status_actions_match_delivery_flow(self):
        dashboard_template = Path("app/templates/couriers/dashboard.html").read_text(encoding="utf-8")
        detail_template = Path("app/templates/couriers/order.html").read_text(encoding="utf-8")

        self.assertIn('/courier/orders/{{ order.id }}/at-courier', dashboard_template)
        self.assertIn('/courier/orders/{{ order.id }}/at-courier', detail_template)
        self.assertIn("У курьера", dashboard_template)
        self.assertIn("Доставлено", dashboard_template)
        self.assertIn('order.status.value == "in_work"', dashboard_template)
        self.assertIn('order.status.value == "at_courier"', dashboard_template)
        self.assertIn("data-courier-delivered-form", detail_template)
        self.assertIn("data-courier-order-status", detail_template)

    def test_courier_dashboard_hides_processed_handover_history(self):
        template = Path("app/templates/couriers/dashboard.html").read_text(encoding="utf-8")

        self.assertIn('selectattr("status.value", "equalto", "pending")', template)
        self.assertIn("Сдач на проверке пока нет", template)
        self.assertNotIn('<div class="fact"><div class="fact-name">Сдано</div>', template)

    def test_courier_dashboard_desktop_has_work_width(self):
        template = Path("app/templates/couriers/dashboard.html").read_text(encoding="utf-8")
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertIn("courier-page-wrap", template)
        self.assertIn("courier-summary-panel", template)
        self.assertIn("Сводка", template)
        self.assertIn(".courier-page-wrap {\n  max-width: var(--content);", styles)
        self.assertIn(".courier-summary-panel {\n  max-width: 760px;", styles)
        self.assertIn("grid-template-columns: repeat(2, minmax(0, 1fr));", styles)
        self.assertIn("@media (min-width: 768px)", styles)
        self.assertIn("max-width: 1120px;", styles)
        self.assertIn(".courier-order-row {\n    grid-template-columns: minmax(0, 1fr) 88px;", styles)
        self.assertIn(".courier-order-actions .action-link {\n    min-height: 32px;", styles)

    def test_client_templates_use_requested_order_counters(self):
        list_template = Path("app/templates/clients/list.html").read_text(encoding="utf-8")
        detail_template = Path("app/templates/clients/detail.html").read_text(encoding="utf-8")

        self.assertIn("Всего заявок", list_template)
        self.assertIn("В работе", list_template)
        self.assertIn("client_total_orders_count(client)", list_template)
        self.assertIn("client_in_work_orders_count(client)", list_template)
        self.assertNotIn("Активные", detail_template)
        self.assertIn("У курьера", detail_template)
        self.assertIn("В архиве", detail_template)

    def test_mobile_cards_shrink_long_cargo_number_without_letter_wrap(self):
        styles = Path("app/static/css/app.css").read_text(encoding="utf-8")

        self.assertRegex(
            styles,
            re.compile(
                r"\.cards\s*\{[^}]*min-width:\s*0;"
                r"[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\);",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            styles,
            re.compile(
                r"\.cargo-pill\s*\{[^}]*white-space:\s*nowrap;"
                r"[^}]*overflow:\s*hidden;"
                r"[^}]*text-overflow:\s*ellipsis;"
                r"[^}]*word-break:\s*normal;",
                re.DOTALL,
            ),
        )
        self.assertIn(".card-top > :first-child { min-width: 0; }", styles)
        self.assertIn(".data-item,\n.value,\n.layout > * { min-width: 0; }", styles)


if __name__ == "__main__":
    unittest.main()
