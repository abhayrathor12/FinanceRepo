# views.py
import json
import base64
import requests
import traceback
import re

from django.http import JsonResponse
from django.shortcuts import get_object_or_404
from django.views.decorators.csrf import csrf_exempt
from django.utils.decorators import method_decorator
from django.views.generic import TemplateView

from rest_framework.views import APIView
from rest_framework.response import Response
from django.core.exceptions import PermissionDenied

from google.generativeai import GenerativeModel, configure

from .models import ChatSession, ChatMessage
from .constants import SYSTEM_PROMPT_FINANCE, API_KEY


# Configure Gemini API
try:
    configure(api_key=API_KEY)
except Exception as config_error:
    print("Failed to configure API:", config_error)


# -------------------------------
# Utility: Clean text for TTS
# -------------------------------
def clean_text_for_tts(text: str) -> str:
    """Remove markdown formatting for TTS output."""
    if not text:
        return text

    # Remove **bold**
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)
    # Remove *italics*
    text = re.sub(r"\*(.*?)\*", r"\1", text)

    return text.strip()


# -------------------------------
# Chat Page View
# -------------------------------
class ChatView(TemplateView):
    template_name = "chat.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user if self.request.user.is_authenticated else None
        session_id = self.request.GET.get("session_id")

        if session_id:
            session = get_object_or_404(ChatSession, id=session_id, user=user)
            context["active_session_id"] = session.id
            context["messages"] = session.messages.all().order_by("created_at")
        else:
            context["active_session_id"] = None
            context["messages"] = []

        context["chat_sessions"] = ChatSession.objects.filter(user=user).order_by("-updated_at")
        return context


# -------------------------------
# MAIN CHAT API
# -------------------------------
@method_decorator(csrf_exempt, name="dispatch")
class ChatAPI(APIView):
    def post(self, request):
        try:
            user = request.user if request.user.is_authenticated else None
            session_id = request.data.get("session_id")
            input_text = request.data.get("message", "").strip()
            file_data = request.data.get("file")
            language = request.data.get("language", "en-US")  # Default English

            # Validate language
            if language not in ["en-US", "hi-IN"]:
                return Response({"error": "Unsupported language."}, status=400)

            # Load or create session
            if session_id:
                session = get_object_or_404(ChatSession, id=session_id, user=user)
            else:
                session = ChatSession.objects.create(user=user, title="New Finance Chat")

            # Save USER message
            if input_text or file_data:
                msg = ChatMessage.objects.create(
                    session=session,
                    is_user=True,
                    text=input_text if input_text else None
                )

                # Save PDF / images
                if file_data:
                    file_data = json.loads(file_data)
                    msg.file_data = file_data["data"]
                    msg.file_mime_type = file_data["mimeType"]
                    msg.save()

            # Choose language instruction
            language_instruction = (
                "Respond in Hindi (हिंदी) and mirror the user's language style."
                if language == "hi-IN"
                else "Respond in English and mirror the user's language style."
            )

            # Prepare chat history
            history = [
                {
                    "role": "user",
                    "parts": [
                        {"text": f"{SYSTEM_PROMPT_FINANCE}\n\n{language_instruction}"}
                    ],
                }
            ]

            for m in session.messages.all().order_by("created_at"):
                role = "user" if m.is_user else "model"
                parts = []

                if m.text:
                    parts.append({"text": m.text})

                if m.file_data:
                    parts.append({
                        "inline_data": {
                            "mime_type": m.file_mime_type,
                            "data": m.file_data,
                        }
                    })

                history.append({"role": role, "parts": parts})

            # Start Gemini chat
            model = GenerativeModel(model_name="gemini-2.5-pro")
            chat = model.start_chat(history=history)

            # Send message to Gemini
            if input_text:
                result = chat.send_message(input_text)
            else:
                result = chat.send_message([
                    {
                        "inline_data": {
                            "mime_type": file_data["mimeType"],
                            "data": file_data["data"],
                        }
                    }
                ])

            # Save MODEL reply
            ChatMessage.objects.create(
                session=session,
                is_user=False,
                text=result.text
            )

            # Auto-generate session title
            if session.title == "New Finance Chat" and input_text:
                words = input_text.split()
                short_title = " ".join(words[:6])
                session.title = short_title[:50]
                session.save()

            tts_text = clean_text_for_tts(result.text)

            return Response({
                "response": result.text,
                "tts_response": tts_text,
                "session_id": session.id,
                "success": True,
            })

        except Exception:
            print("Unexpected Finance AI error:", traceback.format_exc())
            return Response(
                {"error": "Something went wrong. Please try again later.", "success": False},
                status=500
            )


# -------------------------------
# DELETE A CHAT SESSION
# -------------------------------
@method_decorator(csrf_exempt, name="dispatch")
class ChatDeleteAPI(APIView):
    def delete(self, request, session_id):
        try:
            user = request.user if request.user.is_authenticated else None
            session = get_object_or_404(ChatSession, id=session_id, user=user)
            session.delete()
            return Response({"success": True, "message": "Chat session deleted."})

        except PermissionDenied:
            return Response({"success": False, "error": "Access denied."}, status=403)
        except Exception:
            print("Delete error:", traceback.format_exc())
            return Response({"success": False, "error": "Something went wrong."}, status=500)


# -------------------------------
# QR File Downloader (Same as your version)
# -------------------------------
@csrf_exempt
def download_from_qr(request):
    if request.method != "POST":
        return JsonResponse({"error": "Unsupported method."}, status=405)

    try:
        data = json.loads(request.body)
        qr_url = data.get("qr_url")

        if "/d/" not in qr_url:
            return JsonResponse({"error": "Invalid Drive link"}, status=400)

        file_id = qr_url.split("/d/")[1].split("/")[0]
        direct_url = f"https://drive.google.com/uc?export=download&id={file_id}"

        res = requests.get(direct_url)

        if res.status_code == 200:
            file_base64 = base64.b64encode(res.content).decode("utf-8")
            return JsonResponse({
                "success": True,
                "data": file_base64,
                "mime_type": "application/pdf",
                "name": "Downloaded_File.pdf"
            })

        return JsonResponse({"error": "Failed to download file"}, status=500)

    except Exception:
        print("QR Download error:", traceback.format_exc())
        return JsonResponse({"error": "Something went wrong."}, status=500)
