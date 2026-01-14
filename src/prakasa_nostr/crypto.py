import base64
import json
import os
from typing import Optional, Dict, Union

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding

# ==========================================
# NIP-04 (Kind 4) 
# ==========================================
class Nip04Crypto:
    """
    handle Kind 4 messages (Invite / Kick)
    algorithm: AES-256-CBC
    format: Base64(Ciphertext) + "?iv=" + Base64(IV)
    """
    
    @staticmethod
    def encrypt(content: str, shared_secret_hex: str) -> str:
        key = bytes.fromhex(shared_secret_hex)
        iv = os.urandom(16)
        
        padder = padding.PKCS7(128).padder()
        padded_data = padder.update(content.encode('utf-8')) + padder.finalize()
        
        cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
        encryptor = cipher.encryptor()
        ciphertext = encryptor.update(padded_data) + encryptor.finalize()
        
        return f"{base64.b64encode(ciphertext).decode()}?iv={base64.b64encode(iv).decode()}"

    @staticmethod
    def decrypt(encrypted_content: str, shared_secret_hex: str) -> str:
        try:
            key = bytes.fromhex(shared_secret_hex)
            if "?iv=" not in encrypted_content:
                raise ValueError("NIP-04 format error: missing ?iv=")
            
            payload_b64, iv_b64 = encrypted_content.split("?iv=")
            ciphertext = base64.b64decode(payload_b64)
            iv = base64.b64decode(iv_b64)
            
            cipher = Cipher(algorithms.AES(key), modes.CBC(iv), backend=default_backend())
            decryptor = cipher.decryptor()
            padded_data = decryptor.update(ciphertext) + decryptor.finalize()
            
            unpadder = padding.PKCS7(128).unpadder()
            data = unpadder.update(padded_data) + unpadder.finalize()
            return data.decode('utf-8')
        except Exception as e:
            print(f"[Nip04] Decryption failed: {e}")
            raise e

# ==========================================
# Group Crypto (Kind 42 / 20000) encryption and decryption implementation
# ==========================================
class GroupV1Crypto:
    """
    handle Kind 42 (Chat) and Kind 20000 (Reasoning)
    algorithm: AES-256-GCM
    format: Base64( Nonce[12] + Ciphertext + Tag[16] )
    """

    @staticmethod
    def encrypt(json_payload: dict, group_shared_key_hex: str) -> str:
        key = bytes.fromhex(group_shared_key_hex)
        nonce = os.urandom(12)
        plaintext = json.dumps(json_payload).encode('utf-8')
        
        aesgcm = AESGCM(key)
        ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext, None)
        
        final_bytes = nonce + ciphertext_with_tag
        return base64.b64encode(final_bytes).decode('utf-8')

    @staticmethod
    def decrypt(encrypted_b64: str, group_shared_key_hex: str) -> dict:
        try:
            key = bytes.fromhex(group_shared_key_hex)
            data = base64.b64decode(encrypted_b64)
            
            if len(data) < 28: 
                raise ValueError("Data too short")
                
            nonce = data[:12]
            ciphertext_with_tag = data[12:]
            
            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, ciphertext_with_tag, None)
            return json.loads(plaintext.decode('utf-8'))
        except Exception as e:
            print(f"[GroupV1] Decryption failed: {e}")
            raise e

# ==========================================
# Payload builder
# ==========================================
class PayloadBuilder:
    
    @staticmethod
    def build_invite(group_id: str, group_key_hex: str, group_name: str = "My Group") -> dict:
        return {
            "type": "invite",
            "group_id": group_id,
            "key": group_key_hex,      #  group shared key
            "group_name": group_name,
            "name": "Admin"            # invite name
        }

    @staticmethod
    def build_kick(group_id: str) -> dict:
        return {
            "type": "kick",
            "group_id": group_id
        }

    @staticmethod
    def build_chat_message(
        text: str, 
        model: str = "", 
        agent_name: str = "",
        reply_to: Optional[str] = None,
        agent_avatar: Optional[str] = None,
        is_streaming: bool = False
    ) -> dict:
        """
        Build chat message payload for Kind 42 encryption.
        
        Args:
            text: Message text content
            model: Model name used for generation
            agent_name: Agent name
            reply_to: Optional event ID of the message being replied to
            agent_avatar: Optional agent avatar URL or identifier
            is_streaming: Whether this is a streaming chunk (True) or final message (False)
        """
        if is_streaming:
            payload = {
                "text": text,
                "type": "text_chunk",
                "is_streaming": True
            }
        else:
            payload = {
                "text": text,
                "type": "text",
                "is_streaming": False
            }
        if model:
            payload["model"] = model
        if agent_name:
            payload["agent_name"] = agent_name
        if reply_to:
            payload["reply_to"] = reply_to
        if agent_avatar:
            payload["agent_avatar"] = agent_avatar
        return payload

# ==========================================
# Test
# ==========================================
if __name__ == "__main__":
    import secrets
    
    # simulate environment setup
    # 1. assume this is the ECDH shared key between two users (for Kind 4 private message)
    dm_shared_secret = secrets.token_hex(32) 
    
    # 2. assume this is the group shared key (for Kind 42 group chat)
    group_shared_key = secrets.token_hex(32)
    group_id = secrets.token_hex(32) # 64 characters hex string
    
    print(f"[*] DM Secret: {dm_shared_secret[:10]}...")
    print(f"[*] Group Key: {group_shared_key[:10]}...")
    print("-" * 40)

    # --- Scenario A: send Invite (Kind 4) ---
    print(">>> Scenario A: send Invite (Kind 4)")
    invite_data = PayloadBuilder.build_invite(group_id, group_shared_key, "Python Discussion Group")
    invite_json_str = json.dumps(invite_data)
    
    # use NIP-04 encrypt
    encrypted_invite = Nip04Crypto.encrypt(invite_json_str, dm_shared_secret)
    print(f"encrypted Invite message (Kind 4): {encrypted_invite[:50]}...")
    
    # simulate Worker receive and decrypt
    decrypted_invite_str = Nip04Crypto.decrypt(encrypted_invite, dm_shared_secret)
    decrypted_invite = json.loads(decrypted_invite_str)
    print(f"decrypted Invite data: {decrypted_invite}")
    
    # Worker extract group_key
    extracted_group_key = decrypted_invite.get("key")
    assert extracted_group_key == group_shared_key
    print("[Success] successfully pass group key through Invite")
    print("-" * 40)

    # --- Scenario B: group chat message (Kind 42) ---
    print(">>> Scenario B: group chat message (Kind 42)")
    chat_data = PayloadBuilder.build_chat_message("Hello, this is an encrypted message from Python", model="gpt-4o")
    
    # use AES-GCM (Group Key) encrypt
    encrypted_chat = GroupV1Crypto.encrypt(chat_data, extracted_group_key)
    print(f"encrypted Chat message (Kind 42): {encrypted_chat[:50]}...")
    
    # simulate Worker receive and decrypt
    decrypted_chat = GroupV1Crypto.decrypt(encrypted_chat, extracted_group_key)
    print(f"decrypted Chat content: {decrypted_chat}")
    assert decrypted_chat["text"] == "Hello, this is an encrypted message from Python"
    print("[Success] successfully decrypt group message")
    print("-" * 40)

    # --- Scenario C: reasoning process stream (Kind 20000) ---
    print(">>> Scenario C: reasoning process chunk (Kind 20000)")
    # worker.rs logic: {"type": "reasoning_chunk", "text": "..."}
    reasoning_payload = {"type": "reasoning_chunk", "text": "searching related materials..."}
    
    encrypted_reasoning = GroupV1Crypto.encrypt(reasoning_payload, extracted_group_key)
    decrypted_reasoning = GroupV1Crypto.decrypt(encrypted_reasoning, extracted_group_key)
    print(f"decrypted Reasoning: {decrypted_reasoning}")
    assert decrypted_reasoning["text"] == "searching related materials..."
    print("[Success] successfully decrypt reasoning process stream")