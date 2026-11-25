from fastapi import FastAPI
from google import genai
from fastapi.middleware.cors import CORSMiddleware

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Field, Session, SQLModel, create_engine, select, text
from datetime import datetime, timezone
import base64
import uuid
from pathlib import Path
from fastapi.responses import FileResponse
import hashlib
import hmac
import json
import os
import secrets
import time


class Usuario(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    nombre: str
    username: str = Field(unique=True, index=True)
    password_hash: str

class Mensaje(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rol: str
    texto: str
    fecha: str
    conversacion_id: int | None = Field(default=None, foreign_key="conversacion.id")
    usuario_id: int | None = Field(default=None, foreign_key="usuario.id", index=True)
    tipo: str = Field(default="texto")  # "texto" | "imagen"
    imagen_path: str | None = Field(default=None)

class Conversacion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    nombre_conversacion: str
    usuario_id: int | None = Field(default=None, foreign_key="usuario.id", index=True)

sqlite_file_name = "chatbot.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)
    # create_all no agrega columnas a tablas existentes: migracion manual
    with engine.begin() as conn:
        for tabla in ("conversacion", "mensaje"):
            cols = [r[1] for r in conn.execute(text(f"PRAGMA table_info({tabla})"))]
            if "usuario_id" not in cols:
                conn.execute(text(f"ALTER TABLE {tabla} ADD COLUMN usuario_id INTEGER REFERENCES usuario(id)"))
        cols = [r[1] for r in conn.execute(text("PRAGMA table_info(mensaje)"))]
        if "tipo" not in cols:
            conn.execute(text("ALTER TABLE mensaje ADD COLUMN tipo VARCHAR DEFAULT 'texto' NOT NULL"))
        if "imagen_path" not in cols:
            conn.execute(text("ALTER TABLE mensaje ADD COLUMN imagen_path VARCHAR"))

# ---------- Autenticacion ----------
SECRET_FILE = ".auth_secret"
TOKEN_TTL = 60 * 60 * 24 * 7  # 7 dias

def _load_secret() -> bytes:
    env = os.environ.get("AUTH_SECRET")
    if env:
        return env.encode()
    if not os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "w") as f:
            f.write(secrets.token_hex(32))
    with open(SECRET_FILE) as f:
        return f.read().strip().encode()

SECRET = _load_secret()

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return f"{salt.hex()}${dk.hex()}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt_hex, dk_hex = stored.split("$")
    except ValueError:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt_hex), 200_000)
    return hmac.compare_digest(dk.hex(), dk_hex)

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def crear_token(usuario_id: int) -> str:
    payload = _b64(json.dumps({"uid": usuario_id, "exp": int(time.time()) + TOKEN_TTL}).encode())
    firma = _b64(hmac.new(SECRET, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{firma}"

def leer_token(token: str) -> int | None:
    try:
        payload, firma = token.split(".")
        esperada = _b64(hmac.new(SECRET, payload.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(firma, esperada):
            return None
        datos = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        if datos["exp"] < time.time():
            return None
        return int(datos["uid"])
    except Exception:
        return None

bearer = HTTPBearer(auto_error=False)

def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

def get_usuario_actual(
    session: SessionDep,
    cred: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> Usuario:
    uid = leer_token(cred.credentials) if cred else None
    usuario = session.get(Usuario, uid) if uid else None
    if not usuario:
        raise HTTPException(status_code=401, detail="No autenticado")
    return usuario

UsuarioDep = Annotated[Usuario, Depends(get_usuario_actual)]

def conversacion_del_usuario(session: Session, conversacion_id: int, usuario: Usuario) -> Conversacion:
    conv = session.get(Conversacion, conversacion_id)
    if not conv or conv.usuario_id != usuario.id:
        raise HTTPException(status_code=404, detail="Conversacion no encontrada")
    return conv

# Code above omitted 👆

app = FastAPI()


@app.on_event("startup")
def on_startup():
    create_db_and_tables()

# Configurar el middleware de CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # Permite todos los orígenes
    allow_credentials=False,   # ¡Ojo! Debe ser False si usas "*"
    allow_methods=["*"],      # Permite todos los métodos (GET, POST, etc.)
    allow_headers=["*"],      # Permite todos los encabezados
)


client = genai.Client()

class MensajeEntrada(SQLModel):
    texto: str
    conversacion_id: int

class ConversacionName(SQLModel):
    conversacion_id: int
    conversacion_name: str

class RegistroEntrada(SQLModel):
    nombre: str
    username: str
    password: str

class LoginEntrada(SQLModel):
    username: str
    password: str

def _respuesta_auth(u: Usuario):
    return {"token": crear_token(u.id), "usuario": {"id": u.id, "nombre": u.nombre, "username": u.username}}

@app.post("/registro")
def registro(datos: RegistroEntrada, session: SessionDep):
    username = datos.username.strip().lower()
    if len(username) < 3 or len(datos.password) < 6 or not datos.nombre.strip():
        raise HTTPException(status_code=422, detail="Usuario (min 3), nombre y contraseña (min 6) son obligatorios")
    if session.exec(select(Usuario).where(Usuario.username == username)).first():
        raise HTTPException(status_code=409, detail="El usuario ya existe")
    usuario = Usuario(nombre=datos.nombre.strip(), username=username, password_hash=hash_password(datos.password))
    session.add(usuario)
    session.commit()
    session.refresh(usuario)
    return _respuesta_auth(usuario)

@app.post("/login")
def login(datos: LoginEntrada, session: SessionDep):
    usuario = session.exec(select(Usuario).where(Usuario.username == datos.username.strip().lower())).first()
    if not usuario or not verify_password(datos.password, usuario.password_hash):
        raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")
    return _respuesta_auth(usuario)

@app.get("/me")
def me(usuario: UsuarioDep):
    return {"id": usuario.id, "nombre": usuario.nombre, "username": usuario.username}

@app.get("/conversaciones")
def listar_conversaciones(session: SessionDep, usuario: UsuarioDep):
    return session.exec(
        select(Conversacion).where(Conversacion.usuario_id == usuario.id).order_by(Conversacion.id.desc())
    ).all()

@app.post("/conversaciones")
def crear_conversacion(session: SessionDep, usuario: UsuarioDep):
    conversacion = Conversacion(nombre_conversacion = "Nueva Conversacion", usuario_id=usuario.id)
    session.add(conversacion)
    session.commit()
    session.refresh(conversacion)
    return conversacion

@app.post("/chat/") # cambiar a POST
def llamar_ia(mensaje: MensajeEntrada, session: SessionDep, usuario: UsuarioDep):
    conversacion_del_usuario(session, mensaje.conversacion_id, usuario)
    # en esta linea deberíamos guardar el mensaje que llega del humano
    ahora = datetime.now(timezone.utc).isoformat()
    session.add(Mensaje(rol="usuario", texto=mensaje.texto, fecha=ahora, conversacion_id=mensaje.conversacion_id, usuario_id=usuario.id))
    # Recuperamos el historial desde la base de datos para enviarlo como contexto
    session.flush()
    historial = session.exec(select(Mensaje).where(Mensaje.conversacion_id == mensaje.conversacion_id).order_by(Mensaje.id)).all()
    steps = [
        {
            "type": "user_input" if m.rol == "usuario" else "model_output",
            "content": [{"type": "text", "text": f"[Imagen generada a partir de: {m.texto}]" if m.tipo == "imagen" else m.texto}],
        }
        for m in historial
    ]
    inferencia = client.interactions.create(
        model="gemini-3.1-flash-lite",
        input=steps,
        system_instruction=(
            "Eres un asistente de chat solo de texto. No tienes herramientas ni puedes generar imagenes "
            "ni emitir llamadas a funciones. Si el usuario pide una imagen, dile que escriba la descripcion "
            "en el cuadro de texto y pulse el boton 'Imagen' (junto a Enviar). Responde siempre en texto normal."
        ),
    )
    # En esta linea guardamos el mensaje de la IA
    ahora = datetime.now(timezone.utc).isoformat()
    respuesta = Mensaje(rol="bot", texto=inferencia.output_text, fecha=ahora, conversacion_id=mensaje.conversacion_id, usuario_id=usuario.id)
    session.add(respuesta)
    session.commit()
    session.refresh(respuesta)
    return {"Respuesta": inferencia.output_text }

IMAGENES_DIR = Path("imagenes")
MODELO_IMAGEN = "gemini-nano-banana-2.1"
MAX_IMAGENES_DIA = 20
EXTENSIONES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

@app.post("/imagen")
def generar_imagen(entrada: MensajeEntrada, session: SessionDep, usuario: UsuarioDep):
    conversacion_del_usuario(session, entrada.conversacion_id, usuario)
    prompt = entrada.texto.strip()
    if not prompt:
        raise HTTPException(status_code=422, detail="Describe la imagen que quieres generar")

    # limite diario por usuario (control de costo)
    desde = datetime.fromtimestamp(time.time() - 86400, timezone.utc).isoformat()
    recientes = session.exec(
        select(Mensaje).where(
            Mensaje.usuario_id == usuario.id, Mensaje.tipo == "imagen", Mensaje.fecha >= desde
        )
    ).all()
    if len(recientes) >= MAX_IMAGENES_DIA:
        raise HTTPException(status_code=429, detail=f"Limite de {MAX_IMAGENES_DIA} imagenes por dia alcanzado")

    try:
        inferencia = client.interactions.create(model=MODELO_IMAGEN, input=prompt)
        imagen = inferencia.output_image
    except Exception:
        raise HTTPException(status_code=502, detail="El servicio de imagenes fallo, intenta de nuevo")
    if imagen is None or not imagen.data:
        raise HTTPException(status_code=422, detail="El modelo no genero una imagen (puede haber sido bloqueada por seguridad). Prueba otra descripcion.")

    ext = EXTENSIONES.get(getattr(imagen, "mime_type", None) or "image/png", "png")
    carpeta = IMAGENES_DIR / str(usuario.id)
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta = carpeta / f"{uuid.uuid4().hex}.{ext}"
    ruta.write_bytes(base64.b64decode(imagen.data))

    ahora = datetime.now(timezone.utc).isoformat()
    session.add(Mensaje(rol="usuario", texto=f"/imagen {prompt}", fecha=ahora,
                        conversacion_id=entrada.conversacion_id, usuario_id=usuario.id))
    session.flush()
    respuesta = Mensaje(rol="bot", texto=prompt, fecha=ahora, conversacion_id=entrada.conversacion_id,
                        usuario_id=usuario.id, tipo="imagen", imagen_path=str(ruta))
    session.add(respuesta)
    session.commit()
    session.refresh(respuesta)
    return {"mensaje_id": respuesta.id}

@app.get("/imagenes/{mensaje_id}")
def obtener_imagen(mensaje_id: int, session: SessionDep, usuario: UsuarioDep):
    m = session.get(Mensaje, mensaje_id)
    if not m or m.usuario_id != usuario.id or m.tipo != "imagen" or not m.imagen_path:
        raise HTTPException(status_code=404, detail="Imagen no encontrada")
    ruta = Path(m.imagen_path)
    if not ruta.is_file():
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(ruta)

# va a ser necesario definir un endpoint GET que me permita obtener TODOS los mensajes
@app.get("/mensajes/")
def leer_mensajes(
    session: SessionDep,
    usuario: UsuarioDep,
    offset: int = 0,
    limit: Annotated[int, Query(le=100)] = 100,
) -> list[Mensaje]:
    mensajes = session.exec(select(Mensaje).where(Mensaje.usuario_id == usuario.id).offset(offset).limit(limit)).all()
    return mensajes

@app.get("/conversaciones/{conversacion_id}/mensajes")
def mensajes_conversacion(conversacion_id: int, session: SessionDep, usuario: UsuarioDep):
    conversacion_del_usuario(session, conversacion_id, usuario)
    return session.exec(
        select(Mensaje).where(Mensaje.conversacion_id == conversacion_id)
        ).all()

@app.patch("/conversaciones")
def update_conversacion_name(conversacion_name: ConversacionName, session: SessionDep, usuario: UsuarioDep):
    conversacion_db = conversacion_del_usuario(session, conversacion_name.conversacion_id, usuario)
    conversacion = Conversacion(id=conversacion_name.conversacion_id,nombre_conversacion=conversacion_name.conversacion_name)
    conversacion_data = conversacion.model_dump(exclude_unset=True)
    conversacion_db.sqlmodel_update(conversacion_data)
    session.add(conversacion_db)
    session.commit()
    session.refresh(conversacion_db)
    return conversacion_db

@app.delete("/conversaciones/{conversacion_id}")
def eliminar_conversacion(conversacion_id: int, session: SessionDep, usuario: UsuarioDep):
    conversacion = conversacion_del_usuario(session, conversacion_id, usuario)
    mensajes = session.exec(
        select(Mensaje).where(Mensaje.conversacion_id == conversacion_id)
    ).all()
    for m in mensajes:
        if m.tipo == "imagen" and m.imagen_path:
            Path(m.imagen_path).unlink(missing_ok=True)
        session.delete(m)
    session.delete(conversacion)
    session.commit()
    return {"ok": True, "mensajes_eliminados": len(mensajes)}

# SELECT * Mensaje
# WHERE conversacion_id = X