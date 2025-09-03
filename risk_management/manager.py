from config import RISK_SETTINGS
import time

class RiskManager:
    def __init__(self):
        self.daily_loss_limit = RISK_SETTINGS['daily_loss_limit']
        self.max_trade_size = RISK_SETTINGS['max_trade_size']
        self.cooldown_period = RISK_SETTINGS['cooldown_period']
        self.max_positions = RISK_SETTINGS['max_positions']
        
        self.daily_pnl = 0.0
        self.last_trade_time = 0
        self.active_positions = 0
    
    def can_execute_trade(self, opportunity: Dict, available_balance: float) -> tuple:
        """Перевірка чи можна виконувати угоду"""
        # Перевірка щоденного ліміту
        if self.daily_pnl <= self.daily_loss_limit:
            return False, "Денний ліміт втрат досягнуто"
        
        # Перевірка кількості позицій
        if self.active_positions >= self.max_positions:
            return False, "Досягнуто ліміту позицій"
        
        # Перевірка коулдауну
        current_time = time.time()
        if current_time - self.last_trade_time < self.cooldown_period:
            return False, "Коулдаун між угодами"
        
        # Перевірка розміру угоди
        trade_size = available_balance * self.max_trade_size
        if opportunity['buy_price'] * trade_size > available_balance:
            return False, "Недостатньо коштів"
        
        return True, "OK"
    
    def update_after_trade(self, pnl: float):
        """Оновлення стану після угоди"""
        self.daily_pnl += pnl
        self.last_trade_time = time.time()
        self.active_positions += 1
    
    def reset_daily_stats(self):
        """Скидання щоденної статистики"""
        self.daily_pnl = 0.0
        self.active_positions = 0

# Глобальний екземпляр
risk_manager = RiskManager()