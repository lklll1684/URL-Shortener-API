from fastapi import FastAPI, HTTPException, Depends, status
from fastapi.responses import RedirectResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from passlib.context import CryptContext
from jose import JWTError, jwt
from datetime import datetime, timedelta
import random
import string
from sqlalchemy.orm import Session
import redis
import os

from database import init_db, SessionLocal, URLModel, UserModel

app = FastAPI()

init_db()

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
redis_client = redis.Redis(host=REDIS_HOST, port=6379, db=0, decode_responses=True)

SECRET_KEY = "SUPER_SECRET_MEME_KEY_123"
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30
pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def hash_password(password: str):
    return pwd_context.hash(password)

def verify_password(plain_password, hashed_password):
    return pwd_context.verify(plain_password, hashed_password)

def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)

def get_current_user(token: str = Depends(oauth2_scheme), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        username: str = payload.get("sub")
        if username is None:
            raise HTTPException(status_code=404, detail="неверный токен")
    except JWTError:
        raise HTTPException(status_code=404, detail="неверный токен")

    user = db.query(UserModel).filter(UserModel.username == username).first()
    if user is None:
        raise HTTPException(status_code=404, detail="пользователь не найден")
    return user

def generate_short_code(length: int = 6) -> str:
    chars = string.ascii_letters + string.digits
    return ''.join(random.choice(chars) for _ in range(length))

@app.post("/register")
def register(username: str, password: str, db: Session = Depends(get_db)):
    db_user = db.query(UserModel).filter(UserModel.username == username).first()
    if db_user:
        raise HTTPException(status_code=400, detail="такой пользователь уже есть")
    hashed_pwd = hash_password(password)
    new_user = UserModel(username=username, hashed_password=hashed_pwd)
    db.add(new_user)
    db.commit()
    return {"message": "пользователь успешно зарегестрирован"}

@app.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    user = db.query(UserModel).filter(UserModel.username == form_data.username).first()
    if not user or not verify_password(form_data.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="неверный логин или пароль")
    access_token = create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}

@app.post("/shorten")
def shorten_url(long_url: str, db: Session = Depends(get_db), current_user: UserModel = Depends(get_current_user)):
    short_code = generate_short_code()
    db_url = URLModel(long_url=long_url, short_code=short_code, owner_id=current_user.id)
    db.add(db_url)
    db.commit()
    db.refresh(db_url)

    try:
        redis_client.setex(short_code, 86400, long_url)
    except Exception as e:
        print(f"редис недоступен: {e}")

    return {"short_code": db_url.short_code, "short_url": f"http://localhost:8000/{db_url.short_code}"}

@app.get("/my-urls")
def get_my_urls(current_user: UserModel = Depends(get_current_user)):
    return current_user.urls


@app.get("/{short_code}")
def redirect_to_url(short_code: str, db: Session = Depends(get_db)):
    try:
        cached_url = redis_client.get(short_code)
        if cached_url:
            db_url = db.query(URLModel).filter(URLModel.short_code == short_code).first()
            if db_url:
                db_url.clicks_count += 1
                db.commit()

            return RedirectResponse(url=cached_url)
    except Exception:
        pass
    db_url = db.query(URLModel).filter(URLModel.short_code == short_code).first()
    if db_url:
        db_url.clicks_count += 1
        db.commit()
        try:
            redis_client.setex(short_code, 86400, db_url.long_url)
        except Exception:
            pass
        return RedirectResponse(url=db_url.long_url)
    raise HTTPException(status_code=404, detail="ссылка не найдена")

@app.get("/stats/{short_code}")
def redirect_to_url(short_code: str, db: Session = Depends(get_db)):
    db_url = db.query(URLModel).filter(URLModel.short_code == short_code).first()
    if db_url:
        return {
            "short_code": db_url.short_code,
            "long_url": db_url.long_url,
            "clicks_count": db_url.clicks_count,
        }
    raise HTTPException(status_code=404, detail="ссылка не найдена")