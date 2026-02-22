    try:
        response = client.chat.completions.create(...)
        return response
    except Exception as e:
        print(f"Error occurred: {e}")  # Log the actual error
        raise  # Re-raise the exception to avoid hiding the actual error
