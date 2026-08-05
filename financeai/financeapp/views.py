# views.py
import json
import base64
import requests
import traceback
import re
from rest_framework.renderers import JSONRenderer
from rest_framework.parsers import MultiPartParser, FormParser
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
from .constants import (
    SYSTEM_PROMPT_FINANCE,
    API_KEY,
    get_default_pdf_base64,
    DEFAULT_PDF_MIME_TYPE,
)


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
class ChatAPI(APIView):
    renderer_classes = [JSONRenderer]
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        try:
            user = request.user if request.user.is_authenticated else None

            session_id = request.data.get("session_id")
            input_text = request.data.get("message", "").strip()
            file_data_raw = request.data.get("file")
            language = request.data.get("language", "en-US")

            if language not in ["en-US", "hi-IN"]:
                return Response(
                    {"success": False, "error": "Unsupported language"},
                    status=400
                )

            # -------------------------
            # Load/Create Session
            # -------------------------
            if session_id:
                session = get_object_or_404(
                    ChatSession,
                    id=session_id,
                    user=user
                )
                is_new_session = not session.messages.exists()
            else:
                session = ChatSession.objects.create(
                    user=user,
                    title="New Finance Chat"
                )
                is_new_session = True

            # -------------------------
            # Save Current User Message
            # -------------------------
            current_message = ChatMessage.objects.create(
                session=session,
                is_user=True,
                text=input_text if input_text else None
            )

            uploaded_file = None

            if file_data_raw:
                try:
                    uploaded_file = json.loads(file_data_raw)

                    current_message.file_data = uploaded_file["data"]
                    current_message.file_mime_type = uploaded_file["mimeType"]
                    current_message.save()

                except Exception as e:
                    return Response(
                        {
                            "success": False,
                            "error": f"Invalid file payload: {str(e)}"
                        },
                        status=400
                    )
            elif is_new_session:
                # No file attached by user, but this is a new chat —
                # auto-attach the default balance sheet PDF, same as the
                # troubleshooter app does with SERVO_PANEL.pdf
                default_pdf_data = get_default_pdf_base64()
                current_message.file_data = default_pdf_data
                current_message.file_mime_type = DEFAULT_PDF_MIME_TYPE
                current_message.save()
                uploaded_file = {
                    "data": default_pdf_data,
                    "mimeType": DEFAULT_PDF_MIME_TYPE
                }

            # -------------------------
            # Language Instruction
            # -------------------------
            language_instruction = (
                "Respond in Hindi and mirror user's language style."
                if language == "hi-IN"
                else "Respond in English and mirror user's language style."
            )

            # -------------------------
            # Previous History ONLY
            # -------------------------
            history = []

            previous_messages = (
                session.messages
                .exclude(id=current_message.id)
                .order_by("created_at")
            )

            for msg in previous_messages:

                role = "user" if msg.is_user else "model"

                parts = []

                if msg.text:
                    parts.append({"text": msg.text})

                if msg.file_data:
                    parts.append({
                        "inline_data": {
                            "mime_type": msg.file_mime_type,
                            "data": msg.file_data
                        }
                    })

                if parts:
                    history.append({
                        "role": role,
                        "parts": parts
                    })

            # -------------------------
            # Gemini Model
            # -------------------------
            model = GenerativeModel(
                model_name="gemini-2.5-flash",
                system_instruction=f"""
{SYSTEM_PROMPT_FINANCE}

{language_instruction}
"""
            )

            chat = model.start_chat(history=history)

            # -------------------------
            # Build Current Request
            # -------------------------
            current_parts = []

            if input_text:
                current_parts.append(input_text)

            if uploaded_file:
                current_parts.append({
                    "inline_data": {
                        "mime_type": uploaded_file["mimeType"],
                        "data": uploaded_file["data"]
                    }
                })

            if not current_parts:
                return Response(
                    {
                        "success": False,
                        "error": "Message or file required."
                    },
                    status=400
                )

            # -------------------------
            # Send To Gemini
            # -------------------------
            result = chat.send_message(current_parts)

            response_text = (
                result.text
                if hasattr(result, "text")
                else "No response generated."
            )

            # -------------------------
            # Save Assistant Message
            # -------------------------
            ChatMessage.objects.create(
                session=session,
                is_user=False,
                text=response_text
            )

            # -------------------------
            # Auto Title
            # -------------------------
            if (
                session.title == "New Finance Chat"
                and input_text
            ):
                session.title = (
                    " ".join(input_text.split()[:6])[:50]
                )
                session.save()

            return Response({
                "success": True,
                "response": response_text,
                "tts_response": clean_text_for_tts(response_text),
                "session_id": session.id
            })

        except Exception as e:

            print("\n========== GEMINI ERROR ==========")
            print(str(e))
            print(traceback.format_exc())
            print("==================================\n")

            return Response(
                {
                    "success": False,
                    "error": str(e)
                },
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


import os
from django.conf import settings
from django.http import FileResponse, Http404
from rest_framework.views import APIView
from rest_framework.permissions import AllowAny


class PDFPreviewView(APIView):
    """
    Serves financeapp/assets/cfs-Balance-Sheet.pdf so it can be
    opened/rendered inline in a new browser tab.
    """
    permission_classes = [AllowAny]  # tighten later if needed

    def get(self, request):
        pdf_path = os.path.join(settings.BASE_DIR, "financeapp", "assets", "cfs-Balance-Sheet.pdf")

        if not os.path.exists(pdf_path):
            raise Http404("PDF not found.")

        response = FileResponse(open(pdf_path, "rb"), content_type="application/pdf")
        response["Content-Disposition"] = 'inline; filename="cfs-Balance-Sheet.pdf"'
        return response