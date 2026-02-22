    try:
        completion = client.chat.completions.create(...)
    except APIConnectionError as e:
        print(f"APIConnectionError: {e}")
        raise e
    except Exception as e:
        print(f"An error occurred: {e}")
        raise e