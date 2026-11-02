import os
import shutil
import sqlite3
import json
import secrets
import time
from datetime import datetime, timedelta, date
from functools import wraps
from urllib.parse import quote

from flask import Flask, abort, flash, redirect, render_template, request, session, url_for, send_file, jsonify
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.middleware.proxy_fix import ProxyFix

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB = os.getenv('DATABASE_PATH', os.path.join(BASE_DIR, 'reservas.db'))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
WHATSAPP = os.getenv('WHATSAPP_NUMBER', '595986333704')
ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
PENDING_EXPIRE_MINUTES = int(os.getenv('PENDING_EXPIRE_MINUTES', '15'))
SECRETS_FILE = os.path.join(BASE_DIR, '.blink_secrets.json')
INITIAL_PASSWORD_FILE = os.path.join(BASE_DIR, 'CLAVE_ADMIN_INICIAL.txt')

def load_security():
    env_password = os.getenv('ADMIN_PASSWORD')
    env_hash = os.getenv('ADMIN_PASSWORD_HASH')
    env_secret = os.getenv('SECRET_KEY')
    if env_password or env_hash:
        return env_secret or secrets.token_urlsafe(48), env_password or '', env_hash or '', False
    data = {}
    if os.path.exists(SECRETS_FILE):
        try:
            with open(SECRETS_FILE, 'r', encoding='utf-8') as fh: data = json.load(fh)
        except Exception: data = {}
    if not data.get('secret_key') or not data.get('admin_password_hash'):
        password = 'ru461'
        data = {'secret_key': secrets.token_urlsafe(48), 'admin_password_hash': generate_password_hash(password)}
        with open(SECRETS_FILE, 'w', encoding='utf-8') as fh: json.dump(data, fh)
        with open(INITIAL_PASSWORD_FILE, 'w', encoding='utf-8') as fh:
            fh.write('BLINK MOTEL - CLAVE ADMIN INICIAL\nUsuario: admin\nContraseña: '+password+'\n\nEste archivo se elimina automáticamente después del primer acceso correcto.\n')
        print('\n[BLINK MOTEL] Usuario admin: admin')
        print('[BLINK MOTEL] Contraseña inicial guardada en CLAVE_ADMIN_INICIAL.txt\n')
    return data['secret_key'], '', data['admin_password_hash'], True

SECRET_KEY, ADMIN_PASSWORD, ADMIN_PASSWORD_HASH, LOCAL_SECURE_CONFIG = load_security()

app = Flask(__name__)

# Render publica la aplicación detrás de un proxy inverso.
# ProxyFix hace que Flask respete correctamente HTTPS/Host del cliente original
# mediante los encabezados X-Forwarded-* enviados por Render.
app.wsgi_app = ProxyFix(
    app.wsgi_app,
    x_for=1,
    x_proto=1,
    x_host=1,
)

app.secret_key = SECRET_KEY
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax', SESSION_COOKIE_SECURE=os.getenv('HTTPS_ONLY','0')=='1', PERMANENT_SESSION_LIFETIME=timedelta(hours=12))
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 31536000
app.config['TEMPLATES_AUTO_RELOAD'] = False

MOTEL = {
    'name': 'BLINK MOTEL',
    'address': 'Luis Casartelli y Carlos Pastore, San Lorenzo - Tayuazape, Paraguay',
    'reference': 'A dos cuadras de la Ruta 2',
    'hours': 'Abierto las 24 horas',
}

ACTIVE_STATUSES = ('pending', 'confirmed', 'occupied')
ALL_STATUSES = ('pending', 'confirmed', 'occupied', 'completed', 'cancelled', 'no_show')
ROOM_STATES = ('available', 'cleaning', 'maintenance')


def db():
    con = sqlite3.connect(DB, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    con.execute('PRAGMA journal_mode = WAL')
    con.execute('PRAGMA synchronous = NORMAL')
    con.execute('PRAGMA temp_store = MEMORY')
    return con


def col_exists(con, table, col):
    return any(r['name'] == col for r in con.execute(f'PRAGMA table_info({table})'))


def backup_database():
    if not os.path.exists(DB): return
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime('%Y-%m-%d')
    dest = os.path.join(BACKUP_DIR, f'reservas_{stamp}.db')
    if not os.path.exists(dest):
        src = sqlite3.connect(DB); dst = sqlite3.connect(dest)
        try: src.backup(dst)
        finally: dst.close(); src.close()
    cutoff = datetime.now() - timedelta(days=30)
    for name in os.listdir(BACKUP_DIR):
        if name.startswith('reservas_') and name.endswith('.db'):
            path=os.path.join(BACKUP_DIR,name)
            try:
                if datetime.fromtimestamp(os.path.getmtime(path)) < cutoff: os.remove(path)
            except OSError: pass

_last_backup_check = 0
def ensure_daily_backup():
    global _last_backup_check
    now=time.time()
    if now-_last_backup_check > 3600:
        _last_backup_check=now; backup_database()

def expire_pending(con):
    cutoff=(datetime.now()-timedelta(minutes=PENDING_EXPIRE_MINUTES)).isoformat(timespec='seconds')
    rows=con.execute("SELECT id,room_id FROM reservations WHERE status='pending' AND created_at < ?",(cutoff,)).fetchall()
    for r in rows:
        con.execute("UPDATE reservations SET status='cancelled',updated_at=?,notes=CASE WHEN notes='' THEN 'Vencida automáticamente' ELSE notes||' · Vencida automáticamente' END WHERE id=? AND status='pending'",(datetime.now().isoformat(timespec='seconds'),r['id']))
        log_action(con,'vencimiento_automatico',r['room_id'],r['id'],f'Sin confirmar por {PENDING_EXPIRE_MINUTES} min')
    if rows: con.commit()
    return len(rows)


def init_db():
    backup_database()
    con = db()
    con.execute('''CREATE TABLE IF NOT EXISTS rooms (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        base_price INTEGER NOT NULL,
        additional_30 INTEGER NOT NULL,
        operational_state TEXT NOT NULL DEFAULT 'available',
        note TEXT NOT NULL DEFAULT '',
        updated_at TEXT
    )''')
    for room_id in range(1, 16):
        base = 45000 if room_id <= 7 else 50000 if room_id <= 13 else 70000
        extra = 20000 if room_id <= 13 else 30000
        con.execute('''INSERT OR IGNORE INTO rooms
            (id,name,base_price,additional_30,operational_state,note,updated_at)
            VALUES (?,?,?,?, 'available','',?)''',
            (room_id, f'Habitación {room_id}', base, extra, datetime.now().isoformat(timespec='seconds')))

    con.execute('''CREATE TABLE IF NOT EXISTS reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        room_id INTEGER NOT NULL,
        customer_name TEXT NOT NULL,
        phone TEXT NOT NULL,
        start_at TEXT NOT NULL,
        end_at TEXT,
        blocks INTEGER NOT NULL DEFAULT 1,
        duration_text TEXT NOT NULL,
        price INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        created_at TEXT NOT NULL,
        updated_at TEXT,
        duration_minutes INTEGER NOT NULL DEFAULT 90,
        source TEXT NOT NULL DEFAULT 'web',
        check_in_at TEXT,
        check_out_at TEXT,
        extension_minutes INTEGER NOT NULL DEFAULT 0,
        extension_amount INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT ''
    )''')
    migrations = {
        'end_at': 'TEXT', 'updated_at': 'TEXT', 'duration_minutes': 'INTEGER NOT NULL DEFAULT 90',
        'source': "TEXT NOT NULL DEFAULT 'web'", 'check_in_at': 'TEXT', 'check_out_at': 'TEXT',
        'extension_minutes': 'INTEGER NOT NULL DEFAULT 0', 'extension_amount': 'INTEGER NOT NULL DEFAULT 0',
        'notes': "TEXT NOT NULL DEFAULT ''"
    }
    for col, sqltype in migrations.items():
        if not col_exists(con, 'reservations', col):
            con.execute(f'ALTER TABLE reservations ADD COLUMN {col} {sqltype}')

    # Migra reservas antiguas basadas en bloques de 90 minutos.
    rows = con.execute('SELECT id,start_at,end_at,blocks,duration_minutes FROM reservations').fetchall()
    for r in rows:
        minutes = r['duration_minutes'] or (int(r['blocks'] or 1) * 90)
        try:
            start = datetime.fromisoformat(r['start_at'])
            end = datetime.fromisoformat(r['end_at']) if r['end_at'] else start + timedelta(minutes=minutes)
            con.execute('UPDATE reservations SET duration_minutes=?, end_at=?, duration_text=? WHERE id=?',
                        (minutes, end.isoformat(timespec='minutes'), duration_label(minutes), r['id']))
        except (ValueError, TypeError):
            pass

    con.execute('''CREATE TABLE IF NOT EXISTS audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL,
        action TEXT NOT NULL,
        room_id INTEGER,
        reservation_id INTEGER,
        detail TEXT NOT NULL DEFAULT ''
    )''')
    con.execute('CREATE INDEX IF NOT EXISTS idx_res_period ON reservations(room_id,start_at,end_at,status)')
    con.execute('CREATE INDEX IF NOT EXISTS idx_res_start ON reservations(start_at DESC)')
    con.commit(); con.close()


def room_by_id(room_id, con=None):
    own = con is None
    con = con or db()
    row = con.execute('SELECT * FROM rooms WHERE id=?', (room_id,)).fetchone()
    if own: con.close()
    return row


def normalize_phone(value):
    return ''.join(ch for ch in (value or '') if ch.isdigit())


def parse_dt(value):
    return datetime.fromisoformat(value)


def duration_label(minutes):
    h, m = divmod(int(minutes), 60)
    if h and m: return f'{h} h {m} min'
    if h == 1: return '1 hora'
    if h: return f'{h} horas'
    return f'{m} min'


def price_for(room, minutes):
    minutes = int(minutes)
    if minutes < 90 or (minutes - 90) % 30:
        raise ValueError('Duración inválida')
    extras = (minutes - 90) // 30
    return int(room['base_price']) + extras * int(room['additional_30'])


def overlaps(con, room_id, start, end, exclude_id=None):
    sql = '''SELECT id FROM reservations WHERE room_id=?
             AND status IN ('pending','confirmed','occupied')
             AND start_at < ? AND end_at > ?'''
    args = [room_id, end.isoformat(timespec='minutes'), start.isoformat(timespec='minutes')]
    if exclude_id:
        sql += ' AND id != ?'; args.append(exclude_id)
    return con.execute(sql + ' LIMIT 1', args).fetchone() is not None


def log_action(con, action, room_id=None, reservation_id=None, detail=''):
    con.execute('INSERT INTO audit_log(created_at,action,room_id,reservation_id,detail) VALUES(?,?,?,?,?)',
                (datetime.now().isoformat(timespec='seconds'), action, room_id, reservation_id, detail))


def verify_password(password):
    if ADMIN_PASSWORD_HASH:
        return check_password_hash(ADMIN_PASSWORD_HASH, password)
    return password == ADMIN_PASSWORD


def admin_required(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        if not session.get('admin'):
            return redirect(url_for('login', next=request.path))
        return fn(*args, **kwargs)
    return wrapped


def make_whatsapp(reservation, room):
    message = '\n'.join([
        'Hola, quiero confirmar una reserva.',
        f"Reserva N°: {reservation['id']}", MOTEL['name'], f"Habitación: {room['name']}",
        f"Fecha/hora: {parse_dt(reservation['start_at']).strftime('%d/%m/%Y %H:%M')}",
        f"Duración: {reservation['duration_text']}",
        f"Precio: Gs. {int(reservation['price']):,}".replace(',', '.'),
        f"Cliente: {reservation['customer_name']}", f"Teléfono: {reservation['phone']}"
    ])
    return f'https://wa.me/{WHATSAPP}?text={quote(message)}'


def create_reservation(con, room_id, customer, phone, start, minutes, source='web', status='pending', notes=''):
    room = room_by_id(room_id, con)
    if not room: raise ValueError('Habitación inválida.')
    if room['operational_state'] != 'available':
        raise ValueError('La habitación está en limpieza o mantenimiento.')
    expire_pending(con)
    end = start + timedelta(minutes=minutes)
    if overlaps(con, room_id, start, end):
        raise ValueError('La habitación ya está ocupada o reservada en ese horario.')
    price = price_for(room, minutes)
    now = datetime.now().isoformat(timespec='seconds')
    cur = con.execute('''INSERT INTO reservations
      (room_id,customer_name,phone,start_at,end_at,blocks,duration_text,price,status,created_at,updated_at,
       duration_minutes,source,notes)
      VALUES(?,?,?,?,?,1,?,?,?,?,?,?,?,?)''',
      (room_id, customer, phone, start.isoformat(timespec='minutes'), end.isoformat(timespec='minutes'),
       duration_label(minutes), price, status, now, now, minutes, source, notes))
    log_action(con, 'reserva_creada', room_id, cur.lastrowid, f'{source} · {duration_label(minutes)} · Gs. {price}')
    return cur.lastrowid


@app.context_processor
def helpers():
    return {'duration_label': duration_label, 'WHATSAPP': WHATSAPP}

_login_attempts={}
@app.before_request
def maintenance_and_admin_guard():
    if request.endpoint != 'static':
        ensure_daily_backup()

    # Protección básica contra POST administrativos provenientes de otro origen.
    # Con ProxyFix, request.host_url refleja correctamente el HTTPS público de Render.
    if request.method == 'POST' and request.path.startswith('/admin'):
        origin = request.headers.get('Origin')
        if origin:
            from urllib.parse import urlparse
            origin_host = urlparse(origin).netloc
            if origin_host != request.host:
                abort(403)



@app.route('/')
def index():
    con = db(); rooms = con.execute('SELECT * FROM rooms ORDER BY id').fetchall(); con.close()
    return render_template('index.html', rooms=rooms, motel=MOTEL)


@app.get('/api/disponibilidad')
def disponibilidad():
    try:
        start = parse_dt(request.args.get('start_at', ''))
        minutes = int(request.args.get('duration_minutes', '90'))
        if minutes < 90 or (minutes - 90) % 30:
            raise ValueError
        end = start + timedelta(minutes=minutes)
    except (ValueError, TypeError):
        return jsonify({'error': 'Fecha o duración inválida.'}), 400
    con = db(); expire_pending(con)
    # Una sola consulta reemplaza hasta 16 consultas por cada comprobación de disponibilidad.
    unavailable = [row['id'] for row in con.execute('''
        SELECT r.id
        FROM rooms r
        WHERE r.operational_state != 'available'
           OR EXISTS (
                SELECT 1 FROM reservations x
                WHERE x.room_id = r.id
                  AND x.status IN ('pending','confirmed','occupied')
                  AND x.start_at < ?
                  AND x.end_at > ?
           )
        ORDER BY r.id
    ''', (end.isoformat(timespec='minutes'), start.isoformat(timespec='minutes'))).fetchall()]
    con.close()
    response = jsonify({'unavailable_room_ids': unavailable})
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/reservar')
def reservar():
    try:
        room_id = int(request.form.get('room_id', 0)); minutes = int(request.form.get('duration_minutes', 90))
        customer = request.form.get('customer_name', '').strip(); phone = normalize_phone(request.form.get('phone', ''))
        booking_date = request.form.get('booking_date', '').strip()
        booking_hour = request.form.get('booking_hour', '').strip()
        booking_minute = request.form.get('booking_minute', '').strip()

        if not booking_date or booking_hour == '' or booking_minute == '':
            raise ValueError('Seleccioná correctamente la fecha y la hora.')

        try:
            hour = int(booking_hour)
            minute = int(booking_minute)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
            start = datetime.strptime(
                f'{booking_date} {hour:02d}:{minute:02d}',
                '%Y-%m-%d %H:%M'
            )
        except (TypeError, ValueError):
            raise ValueError('Seleccioná correctamente la fecha y la hora.')
        if not customer or len(customer) > 100 or not (7 <= len(phone) <= 15): raise ValueError('Completá correctamente nombre y teléfono.')
        if start < datetime.now() - timedelta(minutes=2): raise ValueError('No se puede reservar un horario que ya pasó.')
        con = db(); con.execute('BEGIN IMMEDIATE')
        rid = create_reservation(con, room_id, customer, phone, start, minutes, 'web', 'pending')
        con.commit(); reservation = con.execute('SELECT * FROM reservations WHERE id=?', (rid,)).fetchone(); room = room_by_id(room_id, con); con.close()
        return render_template('confirmed.html', motel=MOTEL, reservation=reservation, room=room, whatsapp_url=make_whatsapp(reservation, room))
    except (ValueError, TypeError) as e:
        try: con.rollback(); con.close()
        except Exception: pass
        flash(str(e)); return redirect(url_for('index'))
    except sqlite3.Error:
        try: con.rollback(); con.close()
        except Exception: pass
        app.logger.exception('Error al reservar'); flash('No se pudo registrar la reserva.'); return redirect(url_for('index'))


@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        ip=request.remote_addr or 'local'; now=time.time(); attempts=[t for t in _login_attempts.get(ip,[]) if now-t<600]
        if len(attempts)>=5:
            flash('Demasiados intentos. Esperá unos minutos e intentá de nuevo.')
            return render_template('login.html', motel=MOTEL, default_warning=False), 429
        if request.form.get('username') == ADMIN_USER and verify_password(request.form.get('password','')):
            _login_attempts.pop(ip,None); session.clear(); session['admin'] = True; session.permanent = True
            return redirect(request.args.get('next') or url_for('admin'))
        attempts.append(now); _login_attempts[ip]=attempts; flash('Usuario o contraseña incorrectos.')
    return render_template('login.html', motel=MOTEL, default_warning=not bool(ADMIN_PASSWORD_HASH or os.getenv('ADMIN_PASSWORD')))


@app.post('/logout')
def logout():
    session.clear(); return redirect(url_for('login'))


def dashboard_data(con):
    now = datetime.now(); today = date.today().isoformat()
    rooms = con.execute('SELECT * FROM rooms ORDER BY id').fetchall()
    active_rows = con.execute("SELECT * FROM reservations WHERE status IN ('pending','confirmed','occupied') ORDER BY start_at").fetchall()
    board=[]
    counts={'free':0,'reserved':0,'occupied':0,'cleaning':0,'maintenance':0,'ending':0}
    for room in rooms:
        state = room['operational_state']; current=None; upcoming=None
        if state in ('cleaning','maintenance'):
            visual=state
        else:
            # Una habitación SOLO se considera ocupada después de un Check-in real.
            # Que haya llegado la hora de una reserva no cambia automáticamente su estado a ocupada.
            occupied_row = None
            scheduled_now = None
            for r in active_rows:
                if r['room_id'] != room['id']:
                    continue
                try:
                    s = parse_dt(r['start_at']); e = parse_dt(r['end_at'])
                except Exception:
                    continue
                if r['status'] == 'occupied':
                    occupied_row = r
                    break
                if r['status'] in ('pending', 'confirmed'):
                    if s <= now < e:
                        if scheduled_now is None or s < parse_dt(scheduled_now['start_at']):
                            scheduled_now = r
                    elif s > now and (upcoming is None or s < parse_dt(upcoming['start_at'])):
                        upcoming = r
            if occupied_row is not None:
                current = occupied_row
                visual = 'occupied'
            elif scheduled_now is not None:
                current = scheduled_now
                visual = 'reserved'
            elif upcoming is not None:
                current = upcoming
                visual = 'reserved'
            else:
                visual = 'free'
        remaining=None
        if visual=='occupied' and current and current['end_at']:
            remaining=max(0,int((parse_dt(current['end_at'])-now).total_seconds()))
            if remaining <= 15*60: counts['ending'] += 1
        counts[visual]+=1
        board.append({'room':room,'status':visual,'reservation':current,'remaining_seconds':remaining})
    income = con.execute("SELECT COALESCE(SUM(price),0) AS total FROM reservations WHERE substr(start_at,1,10)=? AND status IN ('occupied','completed')", (today,)).fetchone()['total']
    today_count = con.execute("SELECT COUNT(*) AS n FROM reservations WHERE substr(start_at,1,10)=? AND status NOT IN ('cancelled','no_show')", (today,)).fetchone()['n']
    return board, counts, int(income or 0), today_count


@app.route('/admin')
@admin_required
def admin():
    con=db(); expire_pending(con); board, counts, income, today_count = dashboard_data(con)
    status=request.args.get('status',''); q=request.args.get('q','').strip(); only_today=request.args.get('today')=='1'
    sql='SELECT * FROM reservations WHERE 1=1'; args=[]
    if status in ALL_STATUSES: sql+=' AND status=?'; args.append(status)
    if q:
        sql+=' AND (customer_name LIKE ? OR phone LIKE ? OR CAST(id AS TEXT) LIKE ? OR CAST(room_id AS TEXT) LIKE ?)'; args += [f'%{q}%']*4
    if only_today: sql+=' AND substr(start_at,1,10)=?'; args.append(date.today().isoformat())
    reservations=con.execute(sql+' ORDER BY start_at DESC,id DESC LIMIT 300',args).fetchall()
    audits=con.execute('SELECT * FROM audit_log ORDER BY id DESC LIMIT 12').fetchall()
    con.close()
    return render_template('admin.html', motel=MOTEL, room_board=board, counts=counts, income=income,
                           today_count=today_count, reservations=reservations, statuses=ALL_STATUSES,
                           selected_status=status, q=q, only_today=only_today, audits=audits,
                           insecure_default=False)


@app.get('/admin/api/state')
@admin_required
def admin_state():
    con=db(); expired=expire_pending(con); board, counts, income, today_count=dashboard_data(con)
    latest=con.execute("SELECT COALESCE(MAX(id),0) AS id FROM reservations").fetchone()['id']
    latest_update=con.execute("SELECT COALESCE(MAX(updated_at), '') AS u FROM reservations").fetchone()['u']
    ending=[x['room']['id'] for x in board if x['status']=='occupied' and x['remaining_seconds'] is not None and x['remaining_seconds']<=900]
    sig=f"{latest}|{latest_update}|{counts}|{income}|{today_count}|{ending}"
    con.close()
    response=jsonify({'signature':sig,'latest_reservation_id':latest,'ending_room_ids':ending,'expired':expired})
    response.headers['Cache-Control']='no-store'; return response


@app.post('/admin/reservation/manual')
@admin_required
def manual_reservation():
    try:
        room_id=int(request.form['room_id']); minutes=int(request.form['duration_minutes'])
        customer=request.form.get('customer_name','').strip() or 'Cliente recepción'; phone=normalize_phone(request.form.get('phone','')) or '0000000'
        start=parse_dt(request.form.get('start_at') or datetime.now().strftime('%Y-%m-%dT%H:%M'))
        status=request.form.get('status','confirmed'); status = status if status in ('pending','confirmed','occupied') else 'confirmed'
        con=db(); con.execute('BEGIN IMMEDIATE'); rid=create_reservation(con,room_id,customer,phone,start,minutes,'recepcion',status,request.form.get('notes','').strip())
        if status=='occupied': con.execute('UPDATE reservations SET check_in_at=? WHERE id=?',(datetime.now().isoformat(timespec='seconds'),rid))
        con.commit(); con.close(); flash(f'Reserva #{rid} creada correctamente.')
    except Exception as e:
        try: con.rollback(); con.close()
        except Exception: pass
        flash(str(e))
    return redirect(url_for('admin'))


@app.post('/admin/reservation/<int:rid>/action')
@admin_required
def reservation_action(rid):
    action=request.form.get('action',''); con=db(); r=con.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()
    if not r: con.close(); abort(404)
    now=datetime.now(); new_status=None; detail=''
    if action=='confirm': new_status='confirmed'
    elif action=='occupy': new_status='occupied'; con.execute('UPDATE reservations SET check_in_at=COALESCE(check_in_at,?) WHERE id=?',(now.isoformat(timespec='seconds'),rid))
    elif action=='complete':
        new_status='completed'; con.execute('UPDATE reservations SET check_out_at=? WHERE id=?',(now.isoformat(timespec='seconds'),rid))
        con.execute("UPDATE rooms SET operational_state='cleaning',updated_at=? WHERE id=?",(now.isoformat(timespec='seconds'),r['room_id']))
        detail='Pasa automáticamente a limpieza'
    elif action=='cancel': new_status='cancelled'
    elif action=='no_show': new_status='no_show'
    else: con.close(); abort(400)
    con.execute('UPDATE reservations SET status=?,updated_at=? WHERE id=?',(new_status,now.isoformat(timespec='seconds'),rid))
    log_action(con, action, r['room_id'], rid, detail); con.commit(); con.close(); flash(f'Reserva #{rid}: {new_status}.')
    return redirect(url_for('admin'))


@app.post('/admin/reservation/<int:rid>/extend')
@admin_required
def extend_reservation(rid):
    try:
        add=int(request.form.get('minutes',30))
        if add != 30: raise ValueError('Los adicionales son únicamente de 30 minutos.')
        con=db(); con.execute('BEGIN IMMEDIATE'); r=con.execute('SELECT * FROM reservations WHERE id=?',(rid,)).fetchone()
        if not r or r['status'] not in ACTIVE_STATUSES: raise ValueError('La reserva ya no está activa.')
        room=room_by_id(r['room_id'],con); old_end=parse_dt(r['end_at']); new_end=old_end+timedelta(minutes=add)
        if overlaps(con,r['room_id'],old_end,new_end,exclude_id=rid): raise ValueError('No se puede extender: existe otra reserva después.')
        extra_units=add//30; amount=extra_units*int(room['additional_30']); new_minutes=int(r['duration_minutes'])+add
        con.execute('''UPDATE reservations SET end_at=?,duration_minutes=?,duration_text=?,price=price+?,
            extension_minutes=extension_minutes+?,extension_amount=extension_amount+?,updated_at=? WHERE id=?''',
            (new_end.isoformat(timespec='minutes'),new_minutes,duration_label(new_minutes),amount,add,amount,datetime.now().isoformat(timespec='seconds'),rid))
        log_action(con,'extension',r['room_id'],rid,f'+{add} min · +Gs. {amount}')
        con.commit(); con.close(); flash(f'Se agregaron {add} min. Adicional: Gs. {amount:,}'.replace(',','.'))
    except Exception as e:
        try: con.rollback(); con.close()
        except Exception: pass
        flash(str(e))
    return redirect(url_for('admin'))


@app.post('/admin/room/<int:room_id>/state')
@admin_required
def room_state(room_id):
    state=request.form.get('state',''); note=request.form.get('note','').strip()
    if state not in ROOM_STATES: abort(400)
    con=db()
    if state!='available':
        active=con.execute("SELECT id FROM reservations WHERE room_id=? AND status='occupied' LIMIT 1",(room_id,)).fetchone()
        if active: con.close(); flash('No podés poner en mantenimiento/limpieza una habitación ocupada.'); return redirect(url_for('admin'))
    con.execute('UPDATE rooms SET operational_state=?,note=?,updated_at=? WHERE id=?',(state,note,datetime.now().isoformat(timespec='seconds'),room_id))
    log_action(con,f'estado_{state}',room_id,None,note); con.commit(); con.close(); flash(f'Habitación {room_id:02d}: {state}.')
    return redirect(url_for('admin'))


@app.get('/admin/backup')
@admin_required
def download_backup():
    backup_database()
    os.makedirs(BACKUP_DIR,exist_ok=True)
    files=sorted([f for f in os.listdir(BACKUP_DIR) if f.endswith('.db')])
    if not files: abort(404)
    return send_file(os.path.join(BACKUP_DIR,files[-1]),as_attachment=True,download_name=files[-1])


@app.get('/admin/calendar')
@admin_required
def calendar_view():
    day=request.args.get('day',date.today().isoformat())
    try: target=date.fromisoformat(day)
    except ValueError: target=date.today(); day=target.isoformat()
    con=db(); rooms=con.execute('SELECT * FROM rooms ORDER BY id').fetchall(); reservations=con.execute(
        "SELECT * FROM reservations WHERE substr(start_at,1,10)=? AND status NOT IN ('cancelled','no_show') ORDER BY start_at",(day,)).fetchall(); con.close()
    return render_template('calendar.html',motel=MOTEL,rooms=rooms,reservations=reservations,day=day)


@app.errorhandler(400)
def bad_request(_): return render_template('error.html',motel=MOTEL,title='Solicitud inválida',message='No pudimos procesar esa solicitud. Volvé a intentar.'),400
@app.errorhandler(403)
def forbidden(_): return render_template('error.html',motel=MOTEL,title='Acceso no permitido',message='No tenés permiso para realizar esta acción.'),403
@app.errorhandler(404)
def not_found(_): return render_template('error.html',motel=MOTEL,title='Página no encontrada',message='La dirección solicitada no existe.'),404
@app.errorhandler(500)
def server_error(_): return render_template('error.html',motel=MOTEL,title='Ocurrió un inconveniente',message='El sistema no pudo completar la operación. Intentá nuevamente.'),500


init_db()
if __name__=='__main__':
    app.run(host='0.0.0.0',port=5000,debug=os.getenv('FLASK_DEBUG','0')=='1')
