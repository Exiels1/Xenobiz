import streamlit as st
import joblib

# -----------------------------
# Function to explain predictions
# -----------------------------
def explain_prediction(text, model, top_n=3):
    vectorizer = model.named_steps["tfidf"]
    classifier = model.named_steps["clf"]

    # Transform text to 2D vector
    text_vector = vectorizer.transform([text])

    # Predict class using vector
    predicted_class = classifier.predict(text_vector)[0]
    class_index = list(classifier.classes_).index(predicted_class)

    # Multiply vector by classifier coefficients
    coefs = classifier.coef_[class_index]
    word_scores = text_vector.toarray()[0] * coefs

    # Get top keywords
    top_indices = word_scores.argsort()[-top_n:][::-1]
    keywords = [vectorizer.get_feature_names_out()[i] for i in top_indices if word_scores[i] != 0]

    return keywords

# -----------------------------
# Load trained model
# -----------------------------
model = joblib.load("model/sentiment_model.pkl")

# -----------------------------
# Streamlit page setup
# -----------------------------
use_case = st.selectbox(
    "Select context",
    ["Social Media Comment", "Product Review", "Customer Support Message"]
)

st.set_page_config(page_title="Sentiment Analyzer", layout="centered")

st.title("🧠 XenoSentitment")
st.write(
    f"Paste a {use_case.lower()} below and let the AI analyze the sentiment."
)


# -----------------------------
# User input
# -----------------------------
user_input = st.text_area("Enter text here:")

# -----------------------------
# Analyze button
# -----------------------------
if st.button("Analyze Sentiment"):
    if user_input.strip() == "":
        st.warning("Please enter some text.")
    else:
        # Predict sentiment
        prediction = model.predict([user_input])[0]
        probabilities = model.predict_proba([user_input])[0]

        # Confidence logic
        max_prob = probabilities.max()
        confidence = round(max_prob * 100, 2)

        # Emotion layer
        emotion_map = {
            "positive": "Happy 😊",
            "neutral": "Calm 😐",
            "negative": "Frustrated 😠"
        }
        emotion = emotion_map.get(prediction, "Unknown")

        # Explain prediction
        keywords = explain_prediction(user_input, model)

        st.subheader("Why this sentiment?")
        if keywords:
            st.write("Key words influencing the decision:", ", ".join(keywords))
        else:
            st.write("No strong keywords detected.")

        st.subheader("Result")
        st.write(f"**Sentiment:** {prediction.capitalize()}")
        st.write(f"**Emotion:** {emotion}")
        st.write(f"**Confidence:** {confidence}%")

        st.subheader("Detailed Probabilities")
        for label, prob in zip(model.classes_, probabilities):
            st.write(f"{label.capitalize()}: {prob:.2f}")
            
st.markdown(
    "---\n"
    "ℹ️ *This AI provides probabilistic insights, not absolute judgments.*"
)

