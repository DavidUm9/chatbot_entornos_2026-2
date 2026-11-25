from fastapi import FastAPI
from google import genai
from fastapi.middleware.cors import CORSMiddleware

from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlmodel import Field, Session, SQLModel, create_engine, select
from datetime import datetime, timezone


class Mensaje(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    rol: str
    texto: str
    fecha: str
    conversacion_id: int | None = Field(default=None, foreign_key="conversacion.id")

class Conversacion(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    nombre_conversacion: str

sqlite_file_name = "chatbot.db"
sqlite_url = f"sqlite:///{sqlite_file_name}"

connect_args = {"check_same_thread": False}
engine = create_engine(sqlite_url, connect_args=connect_args)

def create_db_and_tables():
    SQLModel.metadata.create_all(engine)

def get_session():
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_session)]

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

@app.get("/conversaciones")
def listar_conversaciones(session: SessionDep):
    return session.exec(select(Conversacion).order_by(Conversacion.id.desc())).all()

@app.post("/conversaciones")
def crear_conversacion(session: SessionDep):
    conversacion = Conversacion()
    session.add(conversacion)
    session.commit()
    session.refresh(conversacion)
    return conversacion

@app.post("/chat/") # cambiar a POST
def llamar_ia(mensaje: MensajeEntrada, session: SessionDep):
    # en esta linea deberíamos guardar el mensaje que llega del humano
    ahora = datetime.now(timezone.utc).isoformat()
    session.add(Mensaje(rol="usuario", texto=mensaje.texto, fecha=ahora, conversacion_id=mensaje.conversacion_id))
    # Recuperamos el historial desde la base de datos para enviarlo como contexto
    session.flush()
    historial = session.exec(select(Mensaje).where(Mensaje.conversacion_id == mensaje.conversacion_id).order_by(Mensaje.id)).all()
    steps = [
        {
            "type": "user_input" if m.rol == "usuario" else "model_output",
            "content": [{"type": "text", "text": m.texto}],
        }
        for m in historial
    ]
    inferencia = client.interactions.create(
        model="gemini-3.1-flash-lite",
        input=steps
    )
    # En esta linea guardamos el mensaje de la IA
    ahora = datetime.now(timezone.utc).isoformat()
    respuesta = Mensaje(rol="bot", texto=inferencia.output_text, fecha=ahora, conversacion_id=mensaje.conversacion_id)
    session.add(respuesta)
    session.commit()
    session.refresh(respuesta)
    return {"Respuesta": inferencia.output_text }

# va a ser necesario definir un endpoint GET que me permita obtener TODOS los mensajes
@app.get("/mensajes/")
def leer_mensajes(
    session: SessionDep,
    offset: int = 0,
    limit: Annotated[int, Query(le=100)] = 100,
) -> list[Mensaje]:
    mensajes = session.exec(select(Mensaje).offset(offset).limit(limit)).all()
    return mensajes

@app.get("/conversaciones/{conversacion_id}/mensajes")
def mensajes_conversacion(conversacion_id: int, session: SessionDep):
    return session.exec(
        select(Mensaje).where(Mensaje.conversacion_id == conversacion_id)
        ).all()

@app.patch("/conversaciones")
def update_conversacion_name(conversacion_name: ConversacionName, session: SessionDep):
    conversacion_db = session.get(Conversacion, conversacion_name.conversacion_id)
    if not conversacion_db:
        raise HTTPException(status_code=404, detail="Conversacion no encontrada")
    conversacion = Conversacion(conversacion_id=conversacion_name.conversacion_id,conversacion_name=conversacion_name.conversacion_name)
    conversacion_data = conversacion.model_dump(exclude_unset=True)
    conversacion_db.sqlmodel_update(conversacion_data)
    session.add(conversacion_db)
    session.commit()
    session.refresh(conversacion_db)
    return conversacion_db

# SELECT * Mensaje
# WHERE conversacion_id = X