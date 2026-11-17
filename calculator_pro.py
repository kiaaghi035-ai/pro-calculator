def calculator():
    print("🧮 Simple Calculator")
    print("Available operations:")
    print("  +  : Addition")
    print("  -  : Subtraction")
    print("  *  : Multiplication")
    print("  /  : Division")
    print("  Type 'q' to quit")
    print("-" * 30)

    while True:
        try:
            # Get operator
            operator = input("\nEnter operator (+, -, *, /) or 'q' to quit: ").strip()

            if operator.lower() == 'q':
                print("👋 Goodbye!")
                break

            if operator not in ['+', '-', '*', '/']:
                print("❌ Invalid operator!")
                continue

            # Get numbers
            num1 = float(input("First number: "))
            num2 = float(input("Second number: "))

            # Perform operation
            if operator == '+':
                result = num1 + num2
            elif operator == '-':
                result = num1 - num2
            elif operator == '*':
                result = num1 * num2
            elif operator == '/':
                if num2 == 0:
                    print("❌ Error: Division by zero is not allowed!")
                    continue
                result = num1 / num2

            print(f"✅ Result: {num1} {operator} {num2} = {result}")

        except ValueError:
            print("❌ Please enter a valid number!")
        except Exception as e:
            print(f"❌ An error occurred: {e}")


if __name__ == "__main__":
    calculator()