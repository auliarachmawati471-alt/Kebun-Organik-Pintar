from decimal import Decimal
from hashlib import sha256
from uuid import uuid4
from django.core import signing
from django.core.exceptions import ValidationError
from django.contrib.auth.decorators import permission_required
from django.http import Http404
from django.shortcuts import redirect
from django.views.decorators.cache import never_cache
from django.conf import settings
from django.db import transaction
from django.db.models import F
from .forms import CheckoutForm
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Sum, Count
from django.shortcuts import render, get_object_or_404
from .models import (
    Product,
    Order,
    Account,
    AccountingPeriod,
    JournalEntry,
    JournalLine,
)
from django.db.models.functions import TruncMonth
from django.utils import timezone
from datetime import date
from django.contrib.admin.views.decorators import staff_member_required


def financial_reports_required(view):
    def validated(request,*args,**kwargs):
        for key in ('period','periode','account'):
            value=request.GET.get(key)
            if value is not None:
                if not value.isdigit() or len(value)>18 or int(value)<1:
                    raise Http404('Filter tidak valid.')
                model=Account if key=='account' else AccountingPeriod
                selected=model.objects.filter(pk=int(value))
                if model is AccountingPeriod:
                    selected=selected.filter(is_legacy=False)
                if not selected.exists():
                    raise Http404('Data filter tidak ditemukan.')
        return view(request,*args,**kwargs)
    return staff_member_required(permission_required('core.view_financial_reports',raise_exception=True)(validated))

def home(request):
    products = Product.objects.filter(is_active=True)

    context = {
        'products': products
    }

    return render(request, 'core/home.html', context)



@never_cache
def checkout(request, product_id):
    product=get_object_or_404(Product.objects.select_related('inventory_item'),pk=product_id,is_active=True)
    token=signing.dumps({'product':product.pk,'nonce':uuid4().hex},salt='checkout')
    form=CheckoutForm(request.POST or None,product=product,initial={'checkout_token':token})
    if request.method=='POST' and form.is_valid():
        data=form.cleaned_data
        key=sha256(data['checkout_token'].encode()).hexdigest()
        try:
            with transaction.atomic():
                order,created=Order.objects.get_or_create(checkout_key=key,defaults=dict(customer_name=data['customer_name'],whatsapp=data['whatsapp'],product=product,quantity=data['quantity'],unit_price=product.price,total_price=product.price*data['quantity']))
            receipt=signing.dumps({'order':order.pk},salt='checkout-success')
            return redirect('checkout_success',token=receipt)
        except ValidationError as error:
            form.add_error(None,error)
    return render(request,'core/checkout.html',{'product':product,'form':form})


@never_cache
def checkout_success(request,token):
    try:
        payload=signing.loads(token,salt='checkout-success',max_age=86400)
    except signing.BadSignature as error:
        raise Http404('Konfirmasi pesanan tidak ditemukan atau kedaluwarsa.') from error
    order=get_object_or_404(Order.objects.select_related('product'),pk=payload.get('order'))
    response=render(request,'core/checkout_success.html',{'order':order,'whatsapp_contact':settings.WHATSAPP_CONTACT})
    response['Referrer-Policy']='no-referrer'
    return response


@financial_reports_required
def admin_dashboard(request):

    total_orders = Order.objects.count()

    waiting_orders = Order.objects.filter(
        status=Order.Status.WAITING
    ).count()

    processing_orders = Order.objects.filter(
        status=Order.Status.PROCESSING
    ).count()

    completed_orders = Order.objects.filter(
        status=Order.Status.COMPLETED, requires_reconciliation=False, sales_journal__isnull=False
    ).count()

    active_products = Product.objects.filter(
        is_active=True
    ).count()

    sales_lines=JournalLine.objects.filter(journal_entry__status='posted',journal_entry__accounting_period__is_legacy=False,journal_entry__entry_type__in=['general','adjustment'],account__account_code__in=['4010','4020','4030'])
    total_sales=sales_lines.aggregate(total=Sum(F('credit')-F('debit')))['total'] or Decimal('0')

    # Menentukan enam bulan terakhir
    today = timezone.localdate()
    month_starts = []

    for offset in range(5, -1, -1):
        month_number = today.year * 12 + today.month - 1 - offset
        year, month_index = divmod(month_number, 12)

        month_starts.append(
            date(year, month_index + 1, 1)
        )

    # Grafik menggunakan tanggal realisasi jurnal, bukan tanggal order dibuat.
    monthly_data=list(sales_lines.filter(journal_entry__entry_date__gte=month_starts[0],journal_entry__entry_date__lte=today).annotate(month=TruncMonth('journal_entry__entry_date')).values('month').annotate(total=Sum(F('credit')-F('debit')),orders=Count('journal_entry',distinct=True)).order_by('month'))

    monthly_lookup = {
        (item['month'].year, item['month'].month): item
        for item in monthly_data
    }

    max_monthly_total = max(
        (item['total'] or 0 for item in monthly_data),
        default=0
    )

    month_names = [
        'Januari', 'Februari', 'Maret',
        'April', 'Mei', 'Juni',
        'Juli', 'Agustus', 'September',
        'Oktober', 'November', 'Desember'
    ]

    monthly_sales = []

    for month_start in month_starts:
        item = monthly_lookup.get(
            (month_start.year, month_start.month),
            {}
        )

        total = item.get('total') or 0
        order_count = item.get('orders', 0)

        bar_height = (
            int(total / max_monthly_total * 100)
            if max_monthly_total else 0
        )

        monthly_sales.append({
            'label': (
                f"{month_names[month_start.month - 1]} "
                f"{month_start.year}"
            ),
            'total': total,
            'orders': order_count,
            'bar_height': max(bar_height, 3) if total else 0
        })

    recent_orders = Order.objects.select_related(
        'product'
    ).order_by('-created_at')[:10]

    context = {
        'total_orders': total_orders,
        'waiting_orders': waiting_orders,
        'processing_orders': processing_orders,
        'completed_orders': completed_orders,
        'active_products': active_products,
        'total_sales': total_sales,
        'recent_orders': recent_orders,
        'monthly_sales': monthly_sales,
    }

    return render(
        request,
        'core/admin_dashboard.html',
        context
    )


    
@financial_reports_required
def general_ledger(request):
    accounts = Account.objects.filter(is_active=True)
    periods = AccountingPeriod.objects.filter(is_legacy=False)

    account_id = request.GET.get("account")
    period_id = request.GET.get("period")

    selected_account = None
    selected_period = None
    ledger_rows = []

    if account_id:
        selected_account = get_object_or_404(
            Account,
            id=account_id
        )

    if period_id:
        selected_period = get_object_or_404(
            AccountingPeriod,
            id=period_id
        )

    if selected_account and selected_period:
        journal_lines = JournalLine.objects.filter(
            account=selected_account,
            journal_entry__accounting_period=selected_period,
            journal_entry__status="posted"
        ).select_related(
            "journal_entry",
            "account"
        ).order_by(
            "journal_entry__entry_date",
            "journal_entry__entry_number", "id"
        )

        balance = 0

        for line in journal_lines:
            if selected_account.normal_balance == "debit":
                balance += line.debit - line.credit
            else:
                balance += line.credit - line.debit

            ledger_rows.append({
                "date": line.journal_entry.entry_date,
                "entry_number": line.journal_entry.entry_number,
                "description": (
                    line.description
                    or line.journal_entry.description
                ),
                "debit": line.debit,
                "credit": line.credit,
                "balance": balance,
            })

    context = {
        "accounts": accounts,
        "periods": periods,
        "selected_account": selected_account,
        "selected_period": selected_period,
        "ledger_rows": ledger_rows,
    }

    return render(
        request,
        "core/general_ledger.html",
        context
    )
@financial_reports_required
def trial_balance(request):
    from .services.financial_report_service import account_balances
    periods=AccountingPeriod.objects.filter(is_legacy=False).order_by('-start_date')
    period_id=request.GET.get('period')
    selected_period=get_object_or_404(periods,pk=period_id) if period_id else periods.first()
    balances=account_balances(selected_period,['opening','general']) if selected_period else {}
    rows=[]
    for account in Account.objects.all():
        value=balances.get(account.pk,Decimal('0'))
        rows.append({'account':account,'debit':max(value,Decimal('0')),'credit':max(-value,Decimal('0'))})
    debit=sum((r['debit'] for r in rows),Decimal('0'));credit=sum((r['credit'] for r in rows),Decimal('0'))
    return render(request,'core/trial_balance.html',dict(periods=periods,selected_period=selected_period,rows=rows,total_debit=debit,total_credit=credit,is_balanced=debit==credit))


@financial_reports_required
def adjusted_trial_balance(request):
    from decimal import Decimal
    from django.db.models import Sum
    from .models import Account, AccountingPeriod, JournalEntry, JournalLine

    periods = AccountingPeriod.objects.filter(is_legacy=False).order_by("-start_date")
    period_id = request.GET.get("period")

    if period_id and period_id.isdigit():
        selected_period = periods.filter(pk=period_id).first()
    else:
        selected_period = periods.first()

    accounts = Account.objects.order_by("account_code")

    rows = []
    total_debit = Decimal("0.00")
    total_credit = Decimal("0.00")

    # Hanya jurnal Posted, sebelum jurnal penutup.
    balances = {}

    if selected_period:
        totals = (
            JournalLine.objects.filter(
                journal_entry__accounting_period=selected_period,
                journal_entry__status=JournalEntry.Status.POSTED,
                journal_entry__entry_type__in=[
                    JournalEntry.EntryType.OPENING,
                    JournalEntry.EntryType.GENERAL,
                    JournalEntry.EntryType.ADJUSTMENT,
                ]
            )
            .values("account_id")
            .annotate(
                debit_total=Sum("debit"),
                credit_total=Sum("credit"),
            )
        )

        balances = {
            item["account_id"]: item
            for item in totals
        }

    for account in accounts:
        data = balances.get(account.pk, {})

        debit = data.get("debit_total") or Decimal("0.00")
        credit = data.get("credit_total") or Decimal("0.00")

        saldo = debit - credit

        saldo_debit = max(saldo, Decimal("0.00"))
        saldo_kredit = max(-saldo, Decimal("0.00"))

        rows.append({
            "account": account,
            "debit": saldo_debit,
            "credit": saldo_kredit,
        })

        total_debit += saldo_debit
        total_credit += saldo_kredit

    context = {
        "periods": periods,
        "selected_period": selected_period,
        "rows": rows,
        "total_debit": total_debit,
        "total_credit": total_credit,
        "is_balanced": total_debit == total_credit,
    }

    return render(
        request,
        "core/adjusted_trial_balance.html",
        context
    )

@financial_reports_required
def worksheet(request):
    from decimal import Decimal
    from django.db.models import Sum
    from .models import (
        Account,
        AccountingPeriod,
        JournalEntry,
        JournalLine,
    )

    zero = Decimal("0.00")

    periods = AccountingPeriod.objects.filter(is_legacy=False).order_by("-start_date")
    period_id = request.GET.get("period")

    if period_id and period_id.isdigit():
        selected_period = periods.filter(pk=period_id).first()
    else:
        selected_period = periods.first()

    accounts = Account.objects.order_by("account_code")

    # Ambil total debit dan kredit berdasarkan jenis jurnal.
    def ambil_saldo(jenis_jurnal):
        if not selected_period:
            return {}

        data = (
            JournalLine.objects.filter(
                journal_entry__accounting_period=selected_period,
                journal_entry__status=JournalEntry.Status.POSTED,
                journal_entry__entry_type__in=jenis_jurnal,
            )
            .values("account_id")
            .annotate(
                jumlah_debit=Sum("debit"),
                jumlah_kredit=Sum("credit"),
            )
        )

        return {
            item["account_id"]: (
                item["jumlah_debit"] or zero,
                item["jumlah_kredit"] or zero,
            )
            for item in data
        }

    # Neraca Saldo sebelum penyesuaian.
    saldo_awal = ambil_saldo([
        JournalEntry.EntryType.OPENING,
        JournalEntry.EntryType.GENERAL,
    ])

    # Jurnal Penyesuaian.
    penyesuaian = ambil_saldo([
        JournalEntry.EntryType.ADJUSTMENT,
    ])

    # Pisahkan saldo bersih menjadi sisi debit dan kredit.
    def pisahkan_saldo(saldo):
        if saldo >= zero:
            return saldo, zero
        return zero, -saldo

    kolom = [
        "ns_debit", "ns_kredit",
        "adj_debit", "adj_kredit",
        "nssp_debit", "nssp_kredit",
        "lr_debit", "lr_kredit",
        "neraca_debit", "neraca_kredit",
    ]

    rows = []
    totals = {nama: zero for nama in kolom}

    for account in accounts:
        debit_awal, kredit_awal = saldo_awal.get(
            account.pk, (zero, zero)
        )

        debit_adj, kredit_adj = penyesuaian.get(
            account.pk, (zero, zero)
        )

        saldo_ns = debit_awal - kredit_awal
        saldo_nssp = saldo_ns + debit_adj - kredit_adj

        ns_debit, ns_kredit = pisahkan_saldo(saldo_ns)
        nssp_debit, nssp_kredit = pisahkan_saldo(saldo_nssp)

        lr_debit = zero
        lr_kredit = zero
        neraca_debit = zero
        neraca_kredit = zero

        # Pendapatan dan beban masuk Laba Rugi.
        if account.category in ("revenue", "expense"):
            lr_debit = nssp_debit
            lr_kredit = nssp_kredit

        # Aset, utang, ekuitas, dan prive masuk Neraca.
        else:
            neraca_debit = nssp_debit
            neraca_kredit = nssp_kredit

        row = {
            "account_code": account.account_code,
            "account_name": account.account_name,
            "ns_debit": ns_debit,
            "ns_kredit": ns_kredit,
            "adj_debit": debit_adj,
            "adj_kredit": kredit_adj,
            "nssp_debit": nssp_debit,
            "nssp_kredit": nssp_kredit,
            "lr_debit": lr_debit,
            "lr_kredit": lr_kredit,
            "neraca_debit": neraca_debit,
            "neraca_kredit": neraca_kredit,
            "is_result": False,
        }

        rows.append(row)

        for nama in kolom:
            totals[nama] += row[nama]

    # Selisih laba rugi untuk menyeimbangkan
    # dua kelompok terakhir pada Neraca Lajur.
    hasil_periode = totals["lr_kredit"] - totals["lr_debit"]

    if hasil_periode != zero:
        hasil_row = {nama: zero for nama in kolom}

        if hasil_periode > zero:
            hasil_row["lr_debit"] = hasil_periode
            hasil_row["neraca_kredit"] = hasil_periode
            nama_hasil = "Laba Bersih Sementara"
        else:
            hasil_row["lr_kredit"] = -hasil_periode
            hasil_row["neraca_debit"] = -hasil_periode
            nama_hasil = "Rugi Bersih Sementara"

        hasil_row.update({
            "account_code": "",
            "account_name": nama_hasil,
            "is_result": True,
        })

        rows.append(hasil_row)

        for nama in kolom:
            totals[nama] += hasil_row[nama]

    is_balanced = all([
        totals["ns_debit"] == totals["ns_kredit"],
        totals["adj_debit"] == totals["adj_kredit"],
        totals["nssp_debit"] == totals["nssp_kredit"],
        totals["lr_debit"] == totals["lr_kredit"],
        totals["neraca_debit"] == totals["neraca_kredit"],
    ])

    context = {
        "periods": periods,
        "selected_period": selected_period,
        "rows": rows,
        "totals": totals,
        "hasil_periode": hasil_periode,
        "is_balanced": is_balanced,
    }

    return render(request, "core/worksheet.html", context)

@financial_reports_required
def laporan_laba_rugi(request):
    from .services.financial_report_service import hitung_laba_rugi

    daftar_periode = AccountingPeriod.objects.filter(is_legacy=False).order_by(
        "-start_date"
    )

    periode_id = request.GET.get("periode")

    if periode_id:
        periode = get_object_or_404(
            AccountingPeriod,
            pk=periode_id
        )
    else:
        periode = get_object_or_404(AccountingPeriod, pk=daftar_periode.values_list("pk",flat=True).first())

    hasil = hitung_laba_rugi(periode)
    from .services.financial_report_service import inventory_reconciliation
    hasil["warnings"] = inventory_reconciliation(periode)["warnings"]

    return render(
        request,
        "core/income_statement.html",
        {
            "periode": periode,
            "daftar_periode": daftar_periode,
            "hasil": hasil,
        }
    )

@financial_reports_required
def laporan_perubahan_ekuitas(request):
    from .services.financial_report_service import (
        hitung_perubahan_ekuitas,
    )

    daftar_periode = AccountingPeriod.objects.filter(is_legacy=False).order_by(
        "-start_date"
    )

    periode_id = request.GET.get("periode")

    if periode_id:
        periode = get_object_or_404(
            AccountingPeriod,
            pk=periode_id,
        )
    else:
        periode = get_object_or_404(AccountingPeriod, pk=daftar_periode.values_list("pk",flat=True).first())

    hasil = hitung_perubahan_ekuitas(periode)

    return render(
        request,
        "core/equity_statement.html",
        {
            "periode": periode,
            "daftar_periode": daftar_periode,
            "hasil": hasil,
        },
    )

@financial_reports_required
def laporan_neraca(request):
    from .services.financial_report_service import hitung_neraca

    daftar_periode = AccountingPeriod.objects.filter(is_legacy=False).order_by(
        "-start_date"
    )

    periode_id = request.GET.get("periode")

    if periode_id:
        periode = get_object_or_404(
            AccountingPeriod,
            pk=periode_id,
        )
    else:
        periode = get_object_or_404(AccountingPeriod, pk=daftar_periode.values_list("pk",flat=True).first())

    hasil = hitung_neraca(periode)

    return render(
        request,
        "core/balance_sheet.html",
        {
            "periode": periode,
            "daftar_periode": daftar_periode,
            "hasil": hasil,
        },
    )

@financial_reports_required
def neraca_saldo_setelah_penutupan(request):
    from .services.financial_report_service import hitung_nspp

    daftar_periode = AccountingPeriod.objects.filter(is_legacy=False).order_by(
        "-start_date"
    )

    periode_id = request.GET.get("periode")

    if periode_id:
        periode = get_object_or_404(
            AccountingPeriod,
            pk=periode_id,
        )
    else:
        periode = get_object_or_404(AccountingPeriod, pk=daftar_periode.values_list("pk",flat=True).first())

    hasil = hitung_nspp(periode)

    return render(
        request,
        "core/post_closing_trial_balance.html",
        {
            "periode": periode,
            "daftar_periode": daftar_periode,
            "hasil": hasil,
        },
    )

@financial_reports_required
def laporan_jurnal_penutup(request):
    from decimal import Decimal
    from .models import JournalEntry, JournalLine

    daftar_periode = AccountingPeriod.objects.filter(is_legacy=False).order_by(
        "-start_date"
    )

    periode_id = request.GET.get("periode")

    if periode_id:
        periode = get_object_or_404(
            AccountingPeriod,
            pk=periode_id,
        )
    else:
        periode = get_object_or_404(AccountingPeriod, pk=daftar_periode.values_list("pk",flat=True).first())

    jurnals = JournalEntry.objects.filter(
        accounting_period=periode,
        entry_type=JournalEntry.EntryType.CLOSING,
        status=JournalEntry.Status.POSTED,
    ).order_by("entry_date", "entry_number")

    daftar_jurnal = []
    total_debit = Decimal("0.00")
    total_kredit = Decimal("0.00")

    for jurnal in jurnals:
        baris = list(
            JournalLine.objects.filter(
                journal_entry=jurnal
            )
            .select_related("account")
            .order_by("id")
        )

        debit_jurnal = sum(
            (item.debit for item in baris),
            Decimal("0.00"),
        )

        kredit_jurnal = sum(
            (item.credit for item in baris),
            Decimal("0.00"),
        )

        daftar_jurnal.append({
            "jurnal": jurnal,
            "baris": baris,
            "total_debit": debit_jurnal,
            "total_kredit": kredit_jurnal,
        })

        total_debit += debit_jurnal
        total_kredit += kredit_jurnal

    return render(
        request,
        "core/closing_journal.html",
        {
            "periode": periode,
            "daftar_periode": daftar_periode,
            "daftar_jurnal": daftar_jurnal,
            "total_debit": total_debit,
            "total_kredit": total_kredit,
            "selisih": total_debit - total_kredit,
        },
    )
