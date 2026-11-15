import os, time
from datetime import datetime
from functools import wraps
from urllib.parse import urlparse
import pg8000.dbapi as pg
from flask import Flask, render_template, request, redirect, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

os.environ['TZ'] = 'Asia/Ho_Chi_Minh'
try: time.tzset()
except Exception: pass
app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-change-me')
app.permanent_session_lifetime = 60*60*24*30
now = lambda: datetime.now().strftime('%d/%m/%Y %H:%M')
STATUS = {'cho': 'Chờ xử lý', 'giao': 'Đang giao', 'xong': 'Hoàn thành', 'huy': 'Đã hủy'}

@app.template_filter('vnd')
def vnd(n): return f"{int(n or 0):,}".replace(',', '.') + 'đ'

def db():
    u = urlparse(os.environ['DATABASE_URL'])
    return pg.connect(user=u.username, password=u.password, host=u.hostname, port=u.port or 5432,
                      database=u.path[1:], ssl_context=True if '.' in (u.hostname or '') else None)

def q(sql, args=(), one=False):
    c = db()
    try:
        cur = c.cursor(); cur.execute(sql, args); rows = None
        if cur.description:
            cols = [d[0] for d in cur.description]
            rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        c.commit()
        return (rows[0] if rows else None) if one else rows
    finally: c.close()

def ledger(cur, uid, d, amt, why):
    cur.execute("INSERT INTO ledger(user_id,dir,amount,reason,created) VALUES(%s,%s,%s,%s,%s)", (uid, d, amt, why, now()))

def init_db():
    for s in ["""CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, phone TEXT UNIQUE, pw TEXT, name TEXT,
        balance BIGINT DEFAULT 0, banned BOOLEAN DEFAULT FALSE, created TEXT)""",
      """CREATE TABLE IF NOT EXISTS products(id SERIAL PRIMARY KEY, name TEXT, price BIGINT, stock INT DEFAULT 0,
        img TEXT DEFAULT '', descr TEXT DEFAULT '', cat TEXT DEFAULT 'Khác', active BOOLEAN DEFAULT TRUE, sold INT DEFAULT 0)""",
      """CREATE TABLE IF NOT EXISTS orders(id SERIAL PRIMARY KEY, user_id INT, total BIGINT, status TEXT DEFAULT 'cho',
        name TEXT, phone TEXT, addr TEXT, note TEXT, created TEXT)""",
      """CREATE TABLE IF NOT EXISTS order_items(id SERIAL PRIMARY KEY, order_id INT, product_id INT, name TEXT, price BIGINT, qty INT)""",
      """CREATE TABLE IF NOT EXISTS deposits(id SERIAL PRIMARY KEY, user_id INT, amount BIGINT, status TEXT DEFAULT 'cho', created TEXT)""",
      """CREATE TABLE IF NOT EXISTS ledger(id SERIAL PRIMARY KEY, user_id INT, dir TEXT, amount BIGINT, reason TEXT, created TEXT)"""]:
        q(s)
    if not q("SELECT 1 FROM products LIMIT 1"):
        for n, p, st, c in [("Tai nghe không dây", 400000, 50, "Điện tử"), ("Bình giữ nhiệt 500ml", 150000, 80, "Gia dụng"), ("Balo chống nước", 320000, 30, "Thời trang")]:
            q("INSERT INTO products(name,price,stock,cat) VALUES(%s,%s,%s,%s)", (n, p, st, c))
try: init_db()
except Exception as e: print('init_db lỗi:', e)

def me():
    if 'uid' not in session: return None
    u = q("SELECT * FROM users WHERE id=%s", (session['uid'],), one=True)
    if not u or u['banned']: session.pop('uid', None); return None
    return u

def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not me(): return redirect('/login')
        return f(*a, **k)
    return w

def admin_required(f):
    @wraps(f)
    def w(*a, **k):
        if not session.get('admin'): return redirect('/admin-VMH/login')
        return f(*a, **k)
    return w

@app.context_processor
def ctx():
    return dict(brand=os.environ.get('BRAND', 'VMH Shop'), user=me() if 'uid' in session else None,
                cart_n=sum(session.get('cart', {}).values()), STATUS=STATUS, bank=dict(
                    name=os.environ.get('BANK_NAME', ''), acc=os.environ.get('BANK_ACC', ''), owner=os.environ.get('BANK_OWNER', '')))

# ---------- Cửa hàng ----------
@app.route('/')
def home():
    cat, kw = request.args.get('cat', ''), request.args.get('q', '').strip()
    sql, a = "SELECT * FROM products WHERE active", []
    if cat: sql += " AND cat=%s"; a.append(cat)
    if kw: sql += " AND name ILIKE %s"; a.append(f"%{kw}%")
    cats = [r['cat'] for r in q("SELECT DISTINCT cat FROM products WHERE active ORDER BY cat")]
    return render_template('shop.html', items=q(sql + " ORDER BY sold DESC, id DESC", a), cats=cats, cat=cat, kw=kw)

@app.post('/cart/add/<int:pid>')
def cart_add(pid):
    c = session.get('cart', {}); c[str(pid)] = c.get(str(pid), 0) + 1; session['cart'] = c
    flash('Đã thêm vào giỏ hàng'); return redirect(request.referrer or '/')

@app.route('/cart', methods=['GET', 'POST'])
def cart():
    c = session.get('cart', {})
    if request.method == 'POST':
        for k in list(c):
            n = int(request.form.get(f'q{k}', c[k]) or 0)
            if n <= 0: c.pop(k)
            else: c[k] = min(n, 99)
        session['cart'] = c; return redirect('/cart')
    rows = q("SELECT * FROM products WHERE id = ANY(%s) AND active", ([int(k) for k in c],)) if c else []
    for r in rows: r['qty'] = c[str(r['id'])]
    return render_template('cart.html', rows=rows, total=sum(r['price'] * r['qty'] for r in rows))

@app.post('/checkout')
@login_required
def checkout():
    c = session.get('cart', {})
    f = {k: request.form.get(k, '').strip() for k in ('name', 'phone', 'addr', 'note')}
    if not c or not (f['name'] and f['phone'] and f['addr']):
        flash('Vui lòng nhập đủ họ tên, số điện thoại và địa chỉ'); return redirect('/cart')
    conn = db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT id,name,price,stock FROM products WHERE id = ANY(%s) AND active FOR UPDATE", ([int(k) for k in c],))
        prods = [dict(zip(('id', 'name', 'price', 'stock'), r)) for r in cur.fetchall()]
        total = 0
        for p in prods:
            p['qty'] = c[str(p['id'])]
            if p['qty'] > p['stock']:
                conn.rollback(); flash(f"“{p['name']}” chỉ còn {p['stock']} sản phẩm"); return redirect('/cart')
            total += p['price'] * p['qty']
        if not prods: conn.rollback(); return redirect('/cart')
        cur.execute("SELECT balance FROM users WHERE id=%s FOR UPDATE", (session['uid'],))
        if cur.fetchone()[0] < total:
            conn.rollback(); flash('Số dư ví không đủ, vui lòng nạp thêm'); return redirect('/wallet')
        cur.execute("INSERT INTO orders(user_id,total,name,phone,addr,note,created) VALUES(%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                    (session['uid'], total, f['name'], f['phone'], f['addr'], f['note'], now()))
        oid = cur.fetchone()[0]
        for p in prods:
            cur.execute("INSERT INTO order_items(order_id,product_id,name,price,qty) VALUES(%s,%s,%s,%s,%s)", (oid, p['id'], p['name'], p['price'], p['qty']))
            cur.execute("UPDATE products SET stock=stock-%s, sold=sold+%s WHERE id=%s", (p['qty'], p['qty'], p['id']))
        cur.execute("UPDATE users SET balance=balance-%s WHERE id=%s", (total, session['uid']))  # trừ tiền DUY NHẤT tại đây
        ledger(cur, session['uid'], 'out', total, f'Thanh toán đơn #{oid}')
        conn.commit()
    finally: conn.close()
    session['cart'] = {}; flash(f'Đặt hàng thành công, mã đơn #{oid}'); return redirect('/orders')

@app.route('/orders')
@login_required
def orders():
    os_ = q("SELECT * FROM orders WHERE user_id=%s ORDER BY id DESC", (session['uid'],))
    for o in os_: o['items'] = q("SELECT * FROM order_items WHERE order_id=%s", (o['id'],))
    return render_template('orders.html', orders=os_)

@app.route('/wallet', methods=['GET', 'POST'])
@login_required
def wallet():
    if request.method == 'POST':
        try: amt = int(request.form.get('amount', 0))
        except ValueError: amt = 0
        if amt < 10000: flash('Số tiền nạp tối thiểu 10.000đ')
        else:
            d = q("INSERT INTO deposits(user_id,amount,created) VALUES(%s,%s,%s) RETURNING id", (session['uid'], amt, now()), one=True)
            flash(f"Đã tạo yêu cầu nạp. Chuyển khoản với nội dung NAP{d['id']}")
        return redirect('/wallet')
    return render_template('wallet.html', deps=q("SELECT * FROM deposits WHERE user_id=%s ORDER BY id DESC LIMIT 10", (session['uid'],)),
                           led=q("SELECT * FROM ledger WHERE user_id=%s ORDER BY id DESC LIMIT 30", (session['uid'],)))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = q("SELECT * FROM users WHERE phone=%s", (request.form.get('phone', '').strip(),), one=True)
        if not u or not check_password_hash(u['pw'], request.form.get('pw', '')): flash('Sai số điện thoại hoặc mật khẩu')
        elif u['banned']: flash('Tài khoản đã bị khóa')
        else: session.permanent = True; session['uid'] = u['id']; return redirect('/')
    return render_template('auth.html', mode='login')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        ph, pw, nm = request.form.get('phone', '').strip(), request.form.get('pw', ''), request.form.get('name', '').strip()
        if not ph.isdigit() or len(ph) < 9 or len(pw) < 6 or not nm: flash('Nhập họ tên, số điện thoại hợp lệ và mật khẩu từ 6 ký tự')
        elif q("SELECT 1 FROM users WHERE phone=%s", (ph,)): flash('Số điện thoại đã được đăng ký')
        else:
            u = q("INSERT INTO users(phone,pw,name,created) VALUES(%s,%s,%s,%s) RETURNING id", (ph, generate_password_hash(pw), nm, now()), one=True)
            session.permanent = True; session['uid'] = u['id']; return redirect('/')
    return render_template('auth.html', mode='register')

@app.route('/logout')
def logout(): session.pop('uid', None); return redirect('/')

# ---------- Quản trị /admin-VMH ----------
@app.route('/admin-VMH/login', methods=['GET', 'POST'])
def admin_login():
    if request.method == 'POST':
        if request.form.get('u') == os.environ.get('ADMIN_USER') and request.form.get('p') == os.environ.get('ADMIN_PASS') and os.environ.get('ADMIN_PASS'):
            session['admin'] = True; return redirect('/admin-VMH/')
        flash('Sai thông tin đăng nhập')
    return render_template('auth.html', mode='admin')

@app.route('/admin-VMH/logout')
def admin_logout(): session.pop('admin', None); return redirect('/admin-VMH/login')

@app.route('/admin-VMH/')
@app.route('/admin-VMH')
@admin_required
def admin_home():
    one = lambda s: q(s, one=True)['n']
    st = [('Người dùng', one("SELECT COUNT(*) n FROM users")), ('Đơn chờ xử lý', one("SELECT COUNT(*) n FROM orders WHERE status='cho'")),
          ('Doanh thu (đơn hoàn thành)', vnd(one("SELECT COALESCE(SUM(total),0) n FROM orders WHERE status='xong'"))),
          ('Nạp chờ duyệt', one("SELECT COUNT(*) n FROM deposits WHERE status='cho'")),
          ('Sản phẩm sắp hết (≤5)', one("SELECT COUNT(*) n FROM products WHERE stock<=5 AND active"))]
    return render_template('admin.html', page='', st=st, top=q("SELECT name,sold FROM products ORDER BY sold DESC LIMIT 5"))

@app.route('/admin-VMH/products', methods=['GET', 'POST'])
@admin_required
def admin_products():
    if request.method == 'POST':
        f, pid = request.form, request.form.get('id')
        v = (f['name'], int(f.get('price') or 0), int(f.get('stock') or 0), f.get('img', ''), f.get('descr', ''), f.get('cat') or 'Khác', 'active' in f)
        if f.get('act') == 'del' and pid: q("DELETE FROM products WHERE id=%s", (int(pid),))
        elif pid: q("UPDATE products SET name=%s,price=%s,stock=%s,img=%s,descr=%s,cat=%s,active=%s WHERE id=%s", v + (int(pid),))
        else: q("INSERT INTO products(name,price,stock,img,descr,cat,active) VALUES(%s,%s,%s,%s,%s,%s,%s)", v)
        flash('Đã lưu'); return redirect('/admin-VMH/products')
    return render_template('admin.html', page='products', rows=q("SELECT * FROM products ORDER BY id DESC"))

@app.route('/admin-VMH/orders', methods=['GET', 'POST'])
@admin_required
def admin_orders():
    if request.method == 'POST':
        oid, new = int(request.form['id']), request.form['status']
        conn = db()
        try:
            cur = conn.cursor(); cur.execute("SELECT status,user_id,total FROM orders WHERE id=%s FOR UPDATE", (oid,)); o = cur.fetchone()
            if o and o[0] not in ('huy', 'xong') and new in STATUS:
                if new == 'huy':  # hoàn tiền + hoàn kho đúng 1 lần
                    cur.execute("UPDATE users SET balance=balance+%s WHERE id=%s", (o[2], o[1])); ledger(cur, o[1], 'in', o[2], f'Hoàn tiền đơn #{oid}')
                    cur.execute("UPDATE products p SET stock=p.stock+i.qty, sold=p.sold-i.qty FROM order_items i WHERE i.order_id=%s AND i.product_id=p.id", (oid,))
                cur.execute("UPDATE orders SET status=%s WHERE id=%s", (new, oid)); conn.commit(); flash('Đã cập nhật đơn')
            else: conn.rollback(); flash('Đơn đã kết thúc, không thể đổi trạng thái')
        finally: conn.close()
        return redirect('/admin-VMH/orders')
    rows = q("SELECT * FROM orders ORDER BY id DESC LIMIT 100")
    for o in rows: o['items'] = q("SELECT * FROM order_items WHERE order_id=%s", (o['id'],))
    return render_template('admin.html', page='orders', rows=rows)

@app.route('/admin-VMH/deposits', methods=['GET', 'POST'])
@admin_required
def admin_deposits():
    if request.method == 'POST':
        did, ok = int(request.form['id']), request.form['act'] == 'ok'
        conn = db()
        try:
            cur = conn.cursor(); cur.execute("SELECT user_id,amount,status FROM deposits WHERE id=%s FOR UPDATE", (did,)); d = cur.fetchone()
            if d and d[2] == 'cho':
                cur.execute("UPDATE deposits SET status=%s WHERE id=%s", ('ok' if ok else 'huy', did))
                if ok: cur.execute("UPDATE users SET balance=balance+%s WHERE id=%s", (d[1], d[0])); ledger(cur, d[0], 'in', d[1], f'Nạp ví NAP{did}')
                conn.commit(); flash('Đã xử lý')
            else: conn.rollback()
        finally: conn.close()
        return redirect('/admin-VMH/deposits')
    return render_template('admin.html', page='deposits', rows=q("SELECT d.*,u.phone FROM deposits d JOIN users u ON u.id=d.user_id ORDER BY d.id DESC LIMIT 100"))

@app.route('/admin-VMH/users', methods=['GET', 'POST'])
@admin_required
def admin_users():
    if request.method == 'POST':
        uid, act = int(request.form['id']), request.form['act']
        if act == 'ban': q("UPDATE users SET banned = NOT banned WHERE id=%s", (uid,))
        elif act == 'adj':
            amt = int(request.form.get('amount') or 0)
            if amt:
                conn = db()
                try:
                    cur = conn.cursor(); cur.execute("UPDATE users SET balance=balance+%s WHERE id=%s", (amt, uid))
                    ledger(cur, uid, 'in' if amt > 0 else 'out', abs(amt), request.form.get('reason') or 'Admin điều chỉnh'); conn.commit()
                finally: conn.close()
        flash('Đã cập nhật'); return redirect('/admin-VMH/users')
    return render_template('admin.html', page='users', rows=q("SELECT * FROM users ORDER BY id DESC LIMIT 200"))

if __name__ == '__main__': app.run(debug=True)
