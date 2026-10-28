import random
from datetime import datetime, timedelta
from sqlalchemy import create_engine, Column, Integer, String, Float, Date, ForeignKey, text
from sqlalchemy.orm import declarative_base, Session

Base = declarative_base()

class Customer(Base):
    __tablename__ = "customers"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    region = Column(String, nullable=False)
    tier = Column(String, nullable=False)

class Product(Base):
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False)
    unit_price = Column(Float, nullable=False)

class Order(Base):
    __tablename__ = "orders"
    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    order_date = Column(Date, nullable=False)
    status = Column(String, nullable=False)

class OrderItem(Base):
    __tablename__ = "order_items"
    id = Column(Integer, primary_key=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=False)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False)
    total_price = Column(Float, nullable=False)

FIRST_NAMES = [
    "James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda",
    "William", "Barbara", "David", "Elizabeth", "Richard", "Susan", "Joseph", "Jessica",
    "Thomas", "Sarah", "Charles", "Karen", "Christopher", "Lisa", "Daniel", "Nancy",
    "Matthew", "Betty", "Anthony", "Margaret", "Mark", "Sandra", "Donald", "Ashley",
    "Steven", "Dorothy", "Paul", "Kimberly", "Andrew", "Emily", "Kenneth", "Donna"
]

LAST_NAMES = [
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis",
    "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson",
    "Thomas", "Taylor", "Moore", "Jackson", "Martin", "Lee", "Perez", "Thompson",
    "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson", "Walker",
    "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores"
]

REGIONS = ["Northeast", "Southeast", "Midwest", "Southwest", "West", "Northwest"]
TIERS = ["Bronze", "Silver", "Gold", "Platinum"]
TIER_WEIGHTS = [0.4, 0.3, 0.2, 0.1]

PRODUCT_DATA = [
    ("Laptop Pro 15", "Electronics", 1299.99),
    ("Laptop Air 13", "Electronics", 999.99),
    ("Wireless Mouse", "Electronics", 29.99),
    ("Mechanical Keyboard", "Electronics", 149.99),
    ("4K Monitor", "Electronics", 499.99),
    ("USB-C Hub", "Electronics", 59.99),
    ("Webcam HD", "Electronics", 89.99),
    ("Noise Canceling Headphones", "Electronics", 299.99),
    ("Bluetooth Speaker", "Electronics", 79.99),
    ("Tablet 10in", "Electronics", 449.99),
    ("Running Shoes", "Clothing", 119.99),
    ("Casual T-Shirt", "Clothing", 24.99),
    ("Denim Jeans", "Clothing", 59.99),
    ("Winter Jacket", "Clothing", 189.99),
    ("Wool Sweater", "Clothing", 79.99),
    ("Sports Shorts", "Clothing", 34.99),
    ("Athletic Socks 6-Pack", "Clothing", 19.99),
    ("Baseball Cap", "Clothing", 29.99),
    ("Leather Belt", "Clothing", 44.99),
    ("Raincoat", "Clothing", 139.99),
    ("Office Desk", "Furniture", 349.99),
    ("Ergonomic Chair", "Furniture", 499.99),
    ("Bookshelf 5-Tier", "Furniture", 199.99),
    ("Filing Cabinet", "Furniture", 149.99),
    ("Standing Desk", "Furniture", 599.99),
    ("Desk Lamp", "Furniture", 49.99),
    ("Whiteboard 48in", "Furniture", 129.99),
    ("Conference Table", "Furniture", 899.99),
    ("Storage Ottoman", "Furniture", 89.99),
    ("Wall Clock", "Furniture", 39.99),
    ("Python Cookbook", "Books", 49.99),
    ("Data Science Handbook", "Books", 59.99),
    ("Machine Learning Guide", "Books", 54.99),
    ("Business Strategy", "Books", 34.99),
    ("Project Management", "Books", 39.99),
    ("SQL Mastery", "Books", 44.99),
    ("Leadership Essentials", "Books", 29.99),
    ("Financial Planning", "Books", 39.99),
    ("Marketing Analytics", "Books", 44.99),
    ("Cloud Architecture", "Books", 54.99),
    ("Coffee Maker Deluxe", "Appliances", 129.99),
    ("Stand Mixer Pro", "Appliances", 349.99),
    ("Air Fryer XL", "Appliances", 99.99),
    ("Blender 1000W", "Appliances", 79.99),
    ("Toaster Oven", "Appliances", 89.99),
    ("Rice Cooker", "Appliances", 59.99),
    ("Electric Kettle", "Appliances", 49.99),
    ("Vacuum Cleaner", "Appliances", 199.99),
    ("Dishwasher Tabs 50pk", "Appliances", 14.99),
    ("Water Filter Pitcher", "Appliances", 39.99),
]

STATUSES = ["completed", "completed", "completed", "shipped", "processing", "cancelled"]

def random_date(start_date: datetime, end_date: datetime) -> datetime:
    delta = end_date - start_date
    random_days = random.randint(0, delta.days)
    return start_date + timedelta(days=random_days)

def seed_database():
    engine = create_engine("sqlite:///business.db", echo=False)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        customers = []
        used_names = set()
        for i in range(1, 151):
            while True:
                fname = random.choice(FIRST_NAMES)
                lname = random.choice(LAST_NAMES)
                full_name = f"{fname} {lname}"
                if full_name not in used_names:
                    used_names.add(full_name)
                    break
            customer = Customer(
                id=i,
                name=full_name,
                region=random.choice(REGIONS),
                tier=random.choices(TIERS, weights=TIER_WEIGHTS, k=1)[0]
            )
            customers.append(customer)
        session.add_all(customers)
        session.flush()

        products = []
        for i, (name, category, price) in enumerate(PRODUCT_DATA, start=1):
            product = Product(
                id=i,
                name=name,
                category=category,
                unit_price=price
            )
            products.append(product)
        session.add_all(products)
        session.flush()

        start_date = datetime(2023, 1, 1)
        end_date = datetime(2024, 6, 30)

        orders = []
        order_items = []
        order_id = 1
        item_id = 1

        for _ in range(500):
            customer = random.choice(customers)
            order_date = random_date(start_date, end_date)
            status = random.choice(STATUSES)

            order = Order(
                id=order_id,
                customer_id=customer.id,
                order_date=order_date.date(),
                status=status
            )
            orders.append(order)

            num_items = random.randint(1, 5)
            selected_products = random.sample(products, min(num_items, len(products)))

            for product in selected_products:
                quantity = random.randint(1, 10)
                total_price = round(quantity * product.unit_price, 2)
                item = OrderItem(
                    id=item_id,
                    order_id=order_id,
                    product_id=product.id,
                    quantity=quantity,
                    total_price=total_price
                )
                order_items.append(item)
                item_id += 1

            order_id += 1

        session.add_all(orders)
        session.add_all(order_items)
        session.commit()

        customer_count = session.execute(text("SELECT COUNT(*) FROM customers")).scalar()
        product_count = session.execute(text("SELECT COUNT(*) FROM products")).scalar()
        order_count = session.execute(text("SELECT COUNT(*) FROM orders")).scalar()
        item_count = session.execute(text("SELECT COUNT(*) FROM order_items")).scalar()

        print(f"Database seeded successfully!")
        print(f"  Customers:   {customer_count}")
        print(f"  Products:    {product_count}")
        print(f"  Orders:      {order_count}")
        print(f"  Order Items: {item_count}")

if __name__ == "__main__":
    seed_database()