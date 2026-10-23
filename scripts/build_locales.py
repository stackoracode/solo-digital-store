"""Source of truth for UI strings. Run `python scripts/build_locales.py` to regenerate app/locales/*.json.

Keeping both languages side by side guarantees identical key sets (enforced again by tests).
"""

import json
from pathlib import Path

S: dict[str, tuple[str, str]] = {
    # navigation / common
    "nav.find_order": ("Find my order", "查询订单"),
    "nav.support": ("Support", "客服"),
    "nav.admin": ("Admin", "管理后台"),
    "nav.account": ("Account", "我的"),
    "nav.sign_in": ("Sign in", "登录"),
    "env.notice": (
        "{env} environment — payments are simulated or in test mode.",
        "{env} 环境 —— 支付为模拟或测试模式。",
    ),
    "footer.support": ("Support: {email}", "客服邮箱：{email}"),
    "common.back_home": ("Back to store", "返回首页"),
    "common.copy": ("Copy", "复制"),
    "common.save": ("Save", "保存"),
    "legal.terms": ("Terms of Service", "服务条款"),
    "legal.privacy": ("Privacy Policy", "隐私政策"),
    "legal.refund": ("Refund Policy", "退款政策"),
    "legal.terms_default": (
        "By purchasing you agree that digital items are delivered electronically to the order page and e-mail you provide. "
        "Do not resell or share delivered items unless the product description allows it. The store may refuse or cancel "
        "orders suspected of fraud. These terms are a draft and must be reviewed for your jurisdiction.",
        "购买即表示您同意：数字商品将以电子方式交付至订单页面及您提供的邮箱。除商品说明允许外，请勿转售或分享已交付内容。"
        "对于疑似欺诈的订单，本店有权拒绝或取消。本条款为草案，需根据实际经营地区审核。",
    ),
    "legal.privacy_default": (
        "We collect only what is needed to fulfil and support your order: your e-mail, order details, the information you "
        "enter in the order form, and technical logs (such as IP address) for fraud prevention. Payments are processed by "
        "our payment providers; we never see full card numbers. You may ask us to delete your account data at any time, "
        "subject to legal record-keeping duties. This policy is a draft and must be reviewed for your jurisdiction.",
        "我们仅收集履行与售后所需的信息：邮箱、订单信息、您在下单表单中填写的内容，以及用于防欺诈的技术日志（如 IP 地址）。"
        "支付由第三方支付机构处理，我们不会获取完整卡号。在法定留存义务允许的范围内，您可随时要求删除账户数据。本政策为草案，需根据实际经营地区审核。",
    ),
    "legal.review_note": (
        "Last reviewed by the store operator. Final terms depend on sales region and product type.",
        "最终条款以实际销售地区与商品类型为准。",
    ),
    # shop
    "shop.search": ("Search", "搜索"),
    "shop.search_placeholder": ("Search products", "搜索商品"),
    "shop.all": ("All", "全部"),
    "shop.out_of_stock": ("Out of stock", "暂时缺货"),
    "shop.no_products": ("No products yet.", "暂无商品。"),
    "fulfillment.auto_card": ("Instant delivery", "自动发货"),
    "fulfillment.manual": ("Manual delivery", "人工发货"),
    "fulfillment.supplier": ("Sourced delivery", "代采发货"),
    "product.delivery_method": ("Delivery", "发货方式"),
    "product.eta": ("Estimated delivery", "预计发货时间"),
    "product.eta_instant": ("Instant after payment", "付款后立即发货"),
    "product.eta_minutes": ("Within {n} minutes", "{n} 分钟内"),
    "product.support": ("Support", "客服"),
    "product.choose_option": ("Choose an option", "选择规格"),
    "product.in_stock": ("{n} in stock", "库存 {n}"),
    "product.quantity": ("Quantity", "数量"),
    "product.email": ("E-mail for delivery", "接收邮箱"),
    "product.email_help": (
        "We send your order link here. No account needed.",
        "订单链接将发送到此邮箱，无需注册。",
    ),
    "product.price_note": (
        "The price shown is the final amount you pay in the currency shown.",
        "显示价格即为最终支付金额（按所示币种）。",
    ),
    "product.buy_now": ("Buy now", "立即购买"),
    "product.guest_ok": (
        "Guest checkout — sign in later to keep your orders together.",
        "支持游客下单，稍后登录即可统一管理订单。",
    ),
    "product.description": ("Description", "商品描述"),
    "product.usage": ("How to use", "使用说明"),
    "product.notes": ("Before you buy", "购买须知"),
    "product.refund_policy": ("Refund policy", "退款政策"),
    # order
    "order.title": ("Order {no}", "订单 {no}"),
    "order.item": ("Item", "商品"),
    "order.total": ("Total", "合计"),
    "order.email": ("E-mail", "邮箱"),
    "order.returned_processing": (
        "Thanks! We are waiting for the payment provider to confirm. This page updates automatically.",
        "感谢付款！正在等待支付机构确认，本页面会自动刷新。",
    ),
    "order.continue_payment": ("Continue payment", "继续支付"),
    "order.choose_payment": ("Choose a payment method", "选择支付方式"),
    "order.no_channels": (
        "No payment method is currently available for this order. Please contact support.",
        "当前订单暂无可用支付方式，请联系客服。",
    ),
    "order.pay_before": ("Please pay before {at}.", "请在 {at} 前完成支付。"),
    "order.your_items": ("Your items", "已发货内容"),
    "order.keep_safe": (
        "Keep these private. Bookmark this page or use the link in your e-mail to come back.",
        "请妥善保管，不要泄露。可收藏本页或通过邮件中的链接再次查看。",
    ),
    "order.timeline": ("Order progress", "订单进度"),
    "order.claim": ("Add this order to my account", "将此订单关联到我的账户"),
    "order.register_prompt": (
        "Sign in with your e-mail to keep all your orders in one place (optional).",
        "使用邮箱登录，即可集中管理您的订单（可选）。",
    ),
    "order.register_cta": ("Sign in / create account", "登录 / 注册"),
    "order.need_help": ("Need help with this order?", "订单遇到问题？"),
    "order.fulfillment_wait.not_started": ("Preparing your order…", "正在准备您的订单…"),
    "order.fulfillment_wait.queued": ("Payment confirmed. Delivering now…", "已确认付款，正在发货…"),
    "order.fulfillment_wait.processing": ("Delivering now…", "正在发货…"),
    "order.fulfillment_wait.awaiting_stock": (
        "Payment confirmed. This item is being restocked; we will deliver as soon as possible or contact you.",
        "已确认付款。商品正在补货，我们会尽快发货或与您联系。",
    ),
    "order.fulfillment_wait.awaiting_manual": (
        "Payment confirmed. Our staff will deliver your item shortly.",
        "已确认付款，工作人员将尽快为您发货。",
    ),
    "order.fulfillment_wait.awaiting_supplier": (
        "Payment confirmed. We are sourcing your item.",
        "已确认付款，正在为您采购。",
    ),
    "order.fulfillment_wait.needs_review": (
        "Delivery is delayed and our team is handling it. We will contact you.",
        "发货出现延迟，工作人员正在处理，会与您联系。",
    ),
    "order.fulfillment_wait.delivered": ("Delivered.", "已发货。"),
    "order.fulfillment_wait.canceled": ("Delivery was cancelled.", "发货已取消。"),
    "status.payment.unpaid": ("Unpaid", "待支付"),
    "status.payment.paid": ("Paid", "已支付"),
    "status.payment.refund_pending": ("Refund in progress", "退款处理中"),
    "status.payment.partially_refunded": ("Partially refunded", "部分退款"),
    "status.payment.refunded": ("Refunded", "已退款"),
    "status.fulfillment.not_started": ("Not started", "未发货"),
    "status.fulfillment.queued": ("Queued", "排队发货"),
    "status.fulfillment.processing": ("Processing", "发货中"),
    "status.fulfillment.awaiting_stock": ("Awaiting stock", "等待补货"),
    "status.fulfillment.awaiting_manual": ("Awaiting staff", "等待人工发货"),
    "status.fulfillment.awaiting_supplier": ("Sourcing", "采购中"),
    "status.fulfillment.needs_review": ("Delayed", "延迟处理中"),
    "status.fulfillment.delivered": ("Delivered", "已发货"),
    "status.fulfillment.canceled": ("Cancelled", "已取消"),
    "status.after_sale.none": ("—", "—"),
    "status.after_sale.open": ("Support open", "售后处理中"),
    "status.after_sale.resolved": ("Support resolved", "售后已解决"),
    "status.lifecycle.expired": ("Expired", "已过期"),
    "status.lifecycle.canceled": ("Cancelled", "已取消"),
    "event.created": ("Order placed", "已下单"),
    "event.payment_started": ("Payment started", "发起支付"),
    "event.paid": ("Payment confirmed", "付款已确认"),
    "event.payment_failed": ("Payment failed", "支付失败"),
    "event.underpaid": ("Amount received is lower than required — under review", "实收金额不足，人工审核中"),
    "event.late_payment": ("Payment received after expiry — under review", "超时到账，人工审核中"),
    "event.awaiting_manual": ("Waiting for staff delivery", "等待人工发货"),
    "event.awaiting_stock": ("Waiting for restock", "等待补货"),
    "event.delivered": ("Delivered", "已发货"),
    "event.procuring": ("Sourcing from supplier", "正在向供应商采购"),
    "event.procurement_pending": ("Confirming with supplier", "正在与供应商确认"),
    "event.procurement_review": ("Delivery delayed — staff handling", "发货延迟，人工处理中"),
    "event.refund_requested": ("Refund requested", "已申请退款"),
    "event.refunded": ("Refunded", "已退款"),
    "event.refund_failed": ("Refund failed — staff will follow up", "退款失败，工作人员将跟进"),
    "event.expired": ("Order expired (unpaid)", "订单已过期（未支付）"),
    "event.note": ("Note", "备注"),
    # usdt
    "usdt.title": ("Pay with {token} on {network}", "使用 {network} 网络的 {token} 支付"),
    "usdt.warning": (
        "Send exactly the amount below, only {token} on {network}, to this one-time address. Other networks, other tokens, "
        "or exchange withdrawals with wrong network can be lost or delayed. Never send from a smart-contract wallet unless it supports this network.",
        "请仅通过 {network} 网络发送 {token}，金额须与下方完全一致，收款地址仅限本订单使用。使用其他网络或其他代币，或交易所提现时选错网络，"
        "可能导致资金丢失或到账延迟。",
    ),
    "usdt.network": ("Network", "网络"),
    "usdt.token": ("Token", "币种"),
    "usdt.contract": ("Token contract", "代币合约"),
    "usdt.amount": ("Exact amount", "应付金额"),
    "usdt.expires": ("Pay before", "请在此时间前支付"),
    "usdt.qr_alt": ("Deposit address QR code", "收款地址二维码"),
    "usdt.copy_address": ("Copy address", "复制地址"),
    "usdt.received": ("Received so far: {amount}", "已收到：{amount}"),
    "usdt.confirmations": ("{n}/{need} confirmations", "确认数 {n}/{need}"),
    "usdt.auto_detect": (
        "Payments are detected on-chain automatically after {n} confirmations. You do not need to submit a transaction hash.",
        "链上到账并达到 {n} 个区块确认后自动识别，无需提交交易哈希。",
    ),
    "usdt.help": (
        "Sent the wrong amount, token or network, or paid late? Contact support — we review every case.",
        "金额、币种或网络发送错误，或超时支付？请联系客服，我们会逐一人工核实。",
    ),
    "chain.seen": ("Detected", "已检测"),
    "chain.confirmed": ("Confirmed", "已确认"),
    "chain.credited": ("Credited", "已入账"),
    "chain.flagged": ("Under review", "人工审核"),
    "chain.reorged": ("Dropped by chain", "已被链上回滚"),
    # lookup
    "lookup.title": ("Find my order", "查询订单"),
    "lookup.help": (
        "Enter your order number and e-mail. We will e-mail you a secure link to the order.",
        "输入订单号与下单邮箱，我们会将订单的安全链接发送到您的邮箱。",
    ),
    "lookup.order_no": ("Order number", "订单号"),
    "lookup.email": ("E-mail used at checkout", "下单邮箱"),
    "lookup.submit": ("Send me the link", "发送链接"),
    "lookup.sent": (
        "If the details match an order, a link is on its way to that e-mail.",
        "如信息匹配，订单链接已发送至该邮箱。",
    ),
    "lookup.returned": (
        "Open the link from your e-mail (or the browser you ordered with) to see your order.",
        "请通过邮件中的链接（或下单时使用的浏览器）查看订单。",
    ),
    # login
    "login.title": ("Sign in", "登录"),
    "login.subtitle": (
        "No password needed. We e-mail you a 6-digit code.",
        "无需密码，我们会向您的邮箱发送 6 位验证码。",
    ),
    "login.email": ("E-mail", "邮箱"),
    "login.send_code": ("Send code", "发送验证码"),
    "login.code_sent": (
        "We sent a code to {email}. It expires in a few minutes.",
        "验证码已发送至 {email}，几分钟内有效。",
    ),
    "login.code": ("6-digit code", "6 位验证码"),
    "login.verify": ("Verify", "验证"),
    "login.change_email": ("Use a different e-mail", "更换邮箱"),
    "login.or": ("or", "或"),
    "login.continue_with": ("Continue with {provider}", "使用 {provider} 登录"),
    "login.guest_note": (
        "You can always buy as a guest without signing in.",
        "无需登录也可直接以游客身份购买。",
    ),
    "staff.title": ("Staff sign in", "员工登录"),
    "staff.password": ("Password", "密码"),
    "staff.sign_in": ("Sign in", "登录"),
    "mfa.title": ("Two-factor verification", "二次验证"),
    "mfa.code": ("Authenticator code", "验证器动态码"),
    "mfa.setup_title": ("Set up two-factor authentication", "设置二次验证"),
    "mfa.setup_help": (
        "Scan the QR code with an authenticator app, then enter the 6-digit code. Required for admin access.",
        "使用验证器 App 扫描二维码并输入 6 位动态码。管理后台必须开启。",
    ),
    "mfa.enable": ("Enable", "启用"),
    # account
    "account.title": ("My account", "我的账户"),
    "account.logout": ("Sign out", "退出登录"),
    "account.orders": ("My orders", "我的订单"),
    "account.no_orders": ("No orders yet.", "暂无订单。"),
    "account.claim_orders": (
        "Add {n} guest order(s) placed with this e-mail",
        "关联此邮箱下的 {n} 个游客订单",
    ),
    "account.profile": ("Profile", "个人资料"),
    "account.display_name": ("Display name", "昵称"),
    "account.language": ("Language", "语言"),
    "account.agent_program": ("Agent program", "推广计划"),
    # support
    "support.title": ("Contact support", "联系客服"),
    "support.email": ("E-mail", "邮箱"),
    "support.hours": ("Hours", "服务时间"),
    "support.order_hint": (
        "Already ordered? Use the help form on your order page so we can see the details.",
        "已下单？请在订单页面提交问题，便于我们查看订单详情。",
    ),
    "support.your_email": ("Your e-mail", "您的邮箱"),
    "support.subject": ("Subject", "主题"),
    "support.message": ("Message", "内容"),
    "support.describe": ("Describe the problem", "请描述问题"),
    "support.send": ("Send", "发送"),
    "ticket.status.open": ("Open", "处理中"),
    "ticket.status.answered": ("Answered", "已回复"),
    "ticket.status.closed": ("Closed", "已关闭"),
    "ticket.author.customer": ("You", "您"),
    "ticket.author.admin": ("Support", "客服"),
    "ticket.author.system": ("System", "系统"),
    "ticket.reply": ("Write a reply", "回复内容"),
    "ticket.bookmark": (
        "Bookmark this page; replies are also e-mailed to you.",
        "请收藏本页，回复也会通过邮件通知您。",
    ),
    # agent
    "agent.title": ("Agent program", "推广计划"),
    "agent.intro": (
        "Share your link and earn a commission on paid orders. Applications are reviewed manually.",
        "分享您的专属链接，订单付款后获得佣金。申请需人工审核。",
    ),
    "agent.note": ("Tell us how you will promote (optional)", "推广方式说明（可选）"),
    "agent.apply": ("Apply", "提交申请"),
    "agent.status.applied": ("Your application is under review.", "您的申请正在审核中。"),
    "agent.status.rejected": ("Your application was not approved.", "您的申请未通过。"),
    "agent.status.suspended": ("Your agent account is suspended.", "您的推广账户已暂停。"),
    "agent.status.active": ("Active", "已启用"),
    "agent.your_link": (
        "Your referral link (last click within 30 days is credited):",
        "您的推广链接（30 天内最后一次点击归因）：",
    ),
    "agent.rate": ("Commission rate", "佣金比例"),
    "agent.orders": ("Paid / attributed orders", "已付款 / 归因订单"),
    "agent.commissions": ("Commissions", "佣金记录"),
    "commission.pending": ("Pending", "待确认"),
    "commission.approved": ("Approved", "已确认"),
    "commission.paid": ("Paid out", "已结算"),
    "commission.reversed": ("Reversed (refund)", "已冲回（退款）"),
    # errors
    "error.product_unavailable": ("This product is not available.", "该商品暂不可购买。"),
    "error.sku_unavailable": ("Please choose an available option.", "请选择可购买的规格。"),
    "error.quantity": ("Please choose a valid quantity.", "请选择有效的数量。"),
    "error.invalid_email": ("Please enter a valid e-mail address.", "请输入有效的邮箱地址。"),
    "error.form_invalid": ("Please check the highlighted fields.", "请检查标红的字段。"),
    "error.out_of_stock": ("Not enough stock for this quantity.", "库存不足。"),
    "error.turnstile": ("Please complete the human verification.", "请完成人机验证。"),
    "error.invalid": ("Something is wrong with the request. Please try again.", "请求有误，请重试。"),
    "error.order_not_payable": ("This order can no longer be paid.", "该订单已无法支付。"),
    "error.order_expired": ("This order has expired. Please place a new order.", "订单已过期，请重新下单。"),
    "error.channel_unavailable": (
        "This payment method is not available for this order.",
        "该支付方式不适用于此订单。",
    ),
    "error.attempt_in_progress": (
        "A payment is already being created. Please wait a moment.",
        "支付正在创建中，请稍候。",
    ),
    "error.provider_error": (
        "The payment provider is temporarily unavailable. Please try again or choose another method.",
        "支付机构暂时不可用，请稍后重试或更换支付方式。",
    ),
    "error.invalid_credentials": ("Incorrect e-mail or password.", "邮箱或密码错误。"),
    "error.locked": ("Too many attempts. Try again later.", "尝试次数过多，请稍后再试。"),
    "error.too_many_codes": (
        "Too many codes requested. Try again later.",
        "验证码请求过于频繁，请稍后再试。",
    ),
    "error.code_invalid": ("The code is invalid or expired.", "验证码无效或已过期。"),
    "error.use_staff_login": ("Staff accounts must use the staff sign-in.", "员工账户请使用员工登录入口。"),
    "form_error.required": ("Required", "必填"),
    "form_error.invalid_option": ("Invalid option", "选项无效"),
    "form_error.invalid": ("Invalid value", "内容无效"),
    "form_error.too_long": ("Too long", "内容过长"),
    "form_error.not_a_number": ("Must be a number", "必须为数字"),
    "form_error.too_small": ("Too small", "数值过小"),
    "form_error.too_large": ("Too large", "数值过大"),
    "form_error.pattern": ("Wrong format", "格式不正确"),
    # admin navigation & dashboard
    "admin.dashboard": ("Dashboard", "概览"),
    "admin.orders": ("Orders", "订单"),
    "admin.reviews": ("Review queue", "人工审核"),
    "admin.products": ("Products", "商品"),
    "admin.inventory": ("Inventory", "库存"),
    "admin.payments": ("Payments", "支付"),
    "admin.tickets": ("Tickets", "工单"),
    "admin.procurements": ("Procurement", "采购"),
    "admin.agents": ("Agents", "代理"),
    "admin.jobs": ("Jobs", "任务"),
    "admin.customers": ("Customers", "用户"),
    "admin.settings": ("Settings", "设置"),
    "admin.audit": ("Audit log", "审计日志"),
    "admin.recent_orders": ("Recent orders", "最新订单"),
    "admin.low_stock": ("Low stock", "库存预警"),
    "admin.quick_list": ("Quick list", "快速上架"),
    "admin.quick_list_help": (
        "Name, price, description and delivery method are enough. Everything else can be added later.",
        "只需填写名称、价格、描述和发货方式，其余信息可稍后补充。",
    ),
    "admin.f.title": ("Name", "名称"),
    "admin.f.price": ("Price", "价格"),
    "admin.f.delivery": ("Delivery method", "发货方式"),
    "admin.f.description": ("Description", "描述"),
    "admin.f.category": ("Category", "分类"),
    "admin.f.image_optional": ("Image (optional)", "图片（可选）"),
    "admin.f.publish_now": ("Publish immediately", "立即上架"),
    "admin.f.list_it": ("List it", "发布"),
    "kpi.visitors": ("Visitors", "访客"),
    "kpi.orders": ("Orders", "订单"),
    "kpi.paid_orders": ("Paid orders", "付款订单"),
    "kpi.conversion": ("Conversion", "转化率"),
    "kpi.payment_success": ("Payment success", "支付成功率"),
    "kpi.delivery_rate": ("Delivered", "发货率"),
    "kpi.avg_delivery": ("Avg. delivery time", "平均发货时长"),
    "kpi.revenue": ("Revenue", "收入"),
    "kpi.refunded": ("Refunded", "退款"),
    "kpi.fees": ("Channel fees", "通道手续费"),
    "kpi.costs": ("Procurement cost", "采购成本"),
    "kpi.commissions": ("Commissions", "佣金"),
    "kpi.contribution": ("Gross contribution", "毛利贡献"),
    "kpi.open_reviews": ("Open reviews", "待审核"),
    # e-mails
    "email.order_paid.subject": ("Payment received for order {order_no}", "订单 {order_no} 已收到付款"),
    "email.order_paid.body": (
        "Thank you! We received your payment of {total} for {product}.\n\nTrack and view your order:\n{link}\n\nKeep this link private.",
        "感谢购买！我们已收到您为「{product}」支付的 {total}。\n\n查看订单：\n{link}\n\n请勿泄露此链接。",
    ),
    "email.order_delivered.subject": ("Your order {order_no} has been delivered", "订单 {order_no} 已发货"),
    "email.order_delivered.body": (
        "Your item ({product}) is ready. Open your order to view it:\n{link}\n\nKeep this link private.",
        "您购买的「{product}」已发货，请打开订单查看：\n{link}\n\n请勿泄露此链接。",
    ),
    "email.order_refunded.subject": ("Refund processed for order {order_no}", "订单 {order_no} 退款已处理"),
    "email.order_refunded.body": (
        "A refund for order {order_no} has been processed. Details:\n{link}",
        "订单 {order_no} 的退款已处理，详情：\n{link}",
    ),
    "email.order_lookup.subject": ("Your link to order {order_no}", "订单 {order_no} 的访问链接"),
    "email.order_lookup.body": (
        "Someone (hopefully you) asked for a link to order {order_no}:\n{link}\n\nIf this wasn't you, you can ignore this e-mail.",
        "有人（希望是您本人）请求了订单 {order_no} 的访问链接：\n{link}\n\n如非本人操作，请忽略此邮件。",
    ),
    "email.ticket_created.subject": ("We received your request {ticket_no}", "已收到您的工单 {ticket_no}"),
    "email.ticket_created.body": (
        'We received your message "{subject}". View or reply here:\n{link}',
        "我们已收到您的问题「{subject}」，查看或回复：\n{link}",
    ),
    "email.ticket_replied.subject": ("New reply to {ticket_no}", "工单 {ticket_no} 有新回复"),
    "email.ticket_replied.body": (
        'Our team replied to "{subject}". Read it here:\n{link}',
        "客服已回复「{subject}」，点击查看：\n{link}",
    ),
    "email.login_code.subject": ("Your sign-in code: {code}", "您的登录验证码：{code}"),
    "email.login_code.body": (
        "Your sign-in code is {code}. It expires in {minutes} minutes.\n\nIf you did not request it, ignore this e-mail. We will never ask you for this code.",
        "您的登录验证码为 {code}，{minutes} 分钟内有效。\n\n如非本人操作请忽略。我们不会以任何方式向您索要此验证码。",
    ),
}


def main() -> None:
    out = Path(__file__).resolve().parent.parent / "app" / "locales"
    out.mkdir(parents=True, exist_ok=True)
    for idx, locale in enumerate(("en-US", "zh-CN")):
        data = {k: v[idx] for k, v in sorted(S.items())}
        (out / f"{locale}.json").write_text(
            json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
