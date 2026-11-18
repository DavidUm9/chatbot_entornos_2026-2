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

@app.get("/chat/{saludo}") # cambiar a POST
def llamar_ia(saludo):
    # en esta linea deberíamos guardar el mensaje que llega del humano
    inferencia = client.interactions.create(
        model="gemini-3.1-flash-lite",
        input=saludo
    )
    # En esta linea guardamos el mensaje de la IA
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
