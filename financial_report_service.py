from decimal import Decimal, ROUND_HALF_UP
from django.core.exceptions import ValidationError
from django.db.models import Sum, Q
from core.models import Account, InventoryItem, InventoryMovement, JournalEntry, JournalLine

ZERO = Decimal('0.00')
BEFORE_CLOSE = ['opening', 'general', 'adjustment']


def amount(value):
    return Decimal(value or 0).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def account_balances(periode, kinds=BEFORE_CLOSE):
    rows = JournalLine.objects.filter(journal_entry__accounting_period=periode, journal_entry__status='posted', journal_entry__entry_type__in=kinds).values('account_id').annotate(debit=Sum('debit'), credit=Sum('credit'))
    return {row['account_id']: amount(row['debit'])-amount(row['credit']) for row in rows}


def hitung_laba_rugi(periode):
    saldo = account_balances(periode, ['general', 'adjustment'])
    accounts = list(Account.objects.all())
    revenue = -sum((saldo.get(a.pk,ZERO) for a in accounts if a.category=='revenue'),ZERO)
    hpp = sum((saldo.get(a.pk,ZERO) for a in accounts if a.account_code=='5050'),ZERO)
    purchases = sum((saldo.get(a.pk,ZERO) for a in accounts if a.account_code in ['5010','5020','5030','5040']),ZERO)
    expenses = {f'{a.account_name} ({a.account_code})':saldo.get(a.pk,ZERO) for a in accounts if a.category=='expense' and a.account_code not in ['5010','5020','5030','5040','5050']}
    # Unallocated purchases remain visible in provisional results, never silently disappear.
    if purchases:
        expenses['Pembelian yang belum dialokasikan'] = purchases
    total = sum(expenses.values(),ZERO)
    return dict(penjualan_bersih=revenue,hpp=hpp,laba_kotor=revenue-hpp,beban=expenses,total_beban=total,laba_bersih=revenue-hpp-total,pembelian_neto_belum_dialokasi=purchases)


def hitung_perubahan_ekuitas(periode):
    opening = account_balances(periode, ['opening'])
    current = account_balances(periode, ['general','adjustment'])
    accounts = list(Account.objects.filter(category='equity'))
    capital = [a for a in accounts if a.normal_balance=='credit']
    drawing = [a for a in accounts if a.normal_balance=='debit']
    initial = -sum((opening.get(a.pk,ZERO) for a in capital),ZERO)
    initial_drawing = sum((opening.get(a.pk,ZERO) for a in drawing),ZERO)
    new = -sum((current.get(a.pk,ZERO) for a in capital),ZERO)
    withdrawals = sum((current.get(a.pk,ZERO) for a in drawing),ZERO)
    profit = hitung_laba_rugi(periode)['laba_bersih']
    return dict(modal_pembukaan=initial,prive_bawaan=initial_drawing,ekuitas_awal=initial-initial_drawing,perubahan_modal=new,prive_periode=withdrawals,laba_rugi=profit,ekuitas_akhir=initial-initial_drawing+new-withdrawals+profit)


def hitung_neraca(periode):
    saldo = account_balances(periode)
    current, fixed, liabilities = {}, {}, {}
    for a in Account.objects.all():
        value = saldo.get(a.pk,ZERO)
        label = f'{a.account_name} ({a.account_code})'
        if a.category=='asset':
            (fixed if a.account_code.startswith('12') else current)[label] = value
        elif a.category=='liability':
            liabilities[label] = -value
    ac, af, liability = sum(current.values(),ZERO),sum(fixed.values(),ZERO),sum(liabilities.values(),ZERO)
    equity = hitung_perubahan_ekuitas(periode)['ekuitas_akhir']
    return dict(aset_lancar=current,aset_tetap=fixed,total_aset_lancar=ac,total_aset_tetap=af,total_aset=ac+af,kewajiban=liabilities,total_kewajiban=liability,total_ekuitas=equity,total_kewajiban_ekuitas=liability+equity,selisih=ac+af-liability-equity)


def inventory_reconciliation(periode):
    from .hpp_service import hitung_hpp_fifo_periodik
    items = InventoryItem.objects.filter(item_type='harvest')
    fifo_hpp, fifo_stock = ZERO, ZERO
    warnings = []
    for item in items:
        result = hitung_hpp_fifo_periodik(item,periode)
        fifo_hpp += amount(result['hpp'])
        fifo_stock += amount(result['nilai_persediaan_akhir'])
    saldo = account_balances(periode)
    codes = {a.account_code:saldo.get(a.pk,ZERO) for a in Account.objects.filter(account_code__in=['1050','5050'])}
    if codes.get('1050',ZERO)!=fifo_stock:
        warnings.append(f"Persediaan akun 1050 {codes.get('1050',ZERO):,.2f} belum sesuai FIFO {fifo_stock:,.2f}.")
    if codes.get('5050',ZERO)!=fifo_hpp:
        warnings.append(f"HPP akun 5050 {codes.get('5050',ZERO):,.2f} belum sesuai FIFO {fifo_hpp:,.2f}. Buat jurnal penyesuaian periodik.")
    return dict(fifo_hpp=fifo_hpp,fifo_stock=fifo_stock,warnings=warnings)


def validate_closing(journal):
    p = journal.accounting_period
    if journal.entry_date != p.end_date:
        raise ValidationError('Jurnal penutup harus bertanggal akhir periode.')
    if p.journal_entries.filter(status='draft').exclude(pk=journal.pk).exists():
        raise ValidationError('Selesaikan semua jurnal Draft sebelum penutupan.')
    if hitung_laba_rugi(p)['pembelian_neto_belum_dialokasi']:
        raise ValidationError('Pembelian periodik belum dialokasikan melalui penyesuaian.')
    try:
        reconciliation = inventory_reconciliation(p)
    except ValueError as error:
        raise ValidationError(str(error)) from error
    if reconciliation['warnings']:
        raise ValidationError(reconciliation['warnings'])
    saldo = account_balances(p)
    for line in journal.lines.all():
        saldo[line.account_id] = saldo.get(line.account_id,ZERO) + line.debit-line.credit
    temporary = Account.objects.filter(Q(category__in=['revenue','expense'])|Q(category='equity',normal_balance='debit'))
    if any(saldo.get(a.pk,ZERO)!=0 for a in temporary):
        raise ValidationError('Jurnal penutup belum menutup seluruh pendapatan, beban, dan prive.')


def hitung_nspp(periode):
    saldo = account_balances(periode,['opening','general','adjustment','closing'])
    rows, temporary = [], []
    debit, credit = ZERO, ZERO
    for a in Account.objects.all():
        value=saldo.get(a.pk,ZERO)
        if value==0:
            continue
        d,c=max(value,ZERO),max(-value,ZERO)
        rows.append(dict(kode=a.account_code,nama=a.account_name,debit=d,kredit=c))
        debit+=d;credit+=c
        if a.category in ['revenue','expense'] or (a.category=='equity' and a.normal_balance=='debit'):
            temporary.append(a.account_code)
    closed = periode.status=='closed' and periode.journal_entries.filter(entry_type='closing',status='posted').exists()
    warnings=[]
    if not closed: warnings.append('Periode belum memiliki penutupan yang sah. Saldo ini masih sementara.')
    if temporary: warnings.append('Masih ada saldo akun temporer: '+', '.join(temporary))
    return dict(daftar_akun=rows,total_debit=debit,total_kredit=credit,selisih=debit-credit,is_final=closed and not temporary,warnings=warnings)
