import os
from cryptography.fernet import Fernet
import base64
from config import SECURITY_SETTINGS

class SecureAPIManager:
    def __init__(self):
        self.cipher_key = SECURITY_SETTINGS['encryption_key'] or self.generate_key()
        self.cipher = Fernet(self.cipher_key)
    
    def generate_key(self) -> str:
        """Генерація ключа шифрування"""
        return Fernet.generate_key().decode()
    
    def encrypt_data(self, data: str) -> str:
        """Шифрування даних"""
        encrypted = self.cipher.encrypt(data.encode())
        return base64.b64encode(encrypted).decode()
    
    def decrypt_data(self, encrypted_data: str) -> str:
        """Розшифрування даних"""
        decoded = base64.b64decode(encrypted_data.encode())
        return self.cipher.decrypt(decoded).decode()
    
    def secure_store_keys(self, exchange: str, api_key: str, secret: str):
        """Безпечне зберігання API ключів"""
        encrypted_key = self.encrypt_data(api_key)
        encrypted_secret = self.encrypt_data(secret)
        
        # Зберігаємо в безпечне місце (базу даних або файл)
        with open(f'secure_{exchange}_keys.txt', 'w') as f:
            f.write(f"{encrypted_key}\n{encrypted_secret}")
    
    def load_secure_keys(self, exchange: str) -> tuple:
        """Завантаження зашифрованих ключів"""
        try:
            with open(f'secure_{exchange}_keys.txt', 'r') as f:
                encrypted_key, encrypted_secret = f.read().splitlines()
            
            api_key = self.decrypt_data(encrypted_key)
            secret = self.decrypt_data(encrypted_secret)
            return api_key, secret
        except FileNotFoundError:
            return None, None

# Глобальний екземпляр
api_manager = SecureAPIManager()